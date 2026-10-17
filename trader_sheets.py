"""Append-only Google Sheets event storage for Render's ephemeral filesystem."""
import base64
import copy
import hashlib
import io
import json
import re
import threading
from pathlib import Path
import gspread
from PIL import Image

HEADERS=['イベントID','記録日時','JAN','操作','商品ID','商品名','型番','参考買取価格(円)','状態','データJSON']

class SheetEventStore:
    def __init__(self,spreadsheet_provider,data_dir,title='トレーダー記録'):
        self.provider=spreadsheet_provider
        self.data_dir=Path(data_dir)
        self.title=title
        self.lock=threading.Lock()
        self.worksheet=None

    def sheet(self):
        if self.worksheet is None:
            book=self.provider()
            try:sheet=book.worksheet(self.title)
            except gspread.exceptions.WorksheetNotFound:
                sheet=book.add_worksheet(title=self.title,rows=1000,cols=len(HEADERS))
            header=sheet.row_values(1)
            if not header:
                sheet.update(range_name='A1:J1',values=[HEADERS],value_input_option='RAW')
            elif header!=HEADERS:
                raise ValueError('トレーダー記録シートの見出しが異なります。既存データ保護のため処理を止めました')
            self.worksheet=sheet
        return self.worksheet

    def pack(self,event):
        event=copy.deepcopy(event)
        product=event['product']
        image_url=product.get('image_url','')
        filename=image_url.removeprefix('/api/trader/image/')
        if image_url and re.fullmatch(r'\d+-[0-9a-f]{32}\.png',filename):
            path=self.data_dir/filename
            if not path.is_file(): raise ValueError('価格画像が見つからないため保存できません。価格を更新してください')
            raw=path.read_bytes()
            product['_image_base64']=base64.b64encode(raw).decode('ascii')
        encoded=json.dumps(event,ensure_ascii=False,separators=(',',':'))
        if len(encoded)>45000:
            raise ValueError('価格画像を含む記録が大きすぎるためGoogle Sheetsへ保存できません')
        return encoded

    def unpack(self,encoded):
        event=json.loads(encoded)
        if event.get('schema')!=1 or event.get('kind') not in ('select','quote'):
            raise ValueError('未対応の保存形式です')
        if not re.fullmatch(r'\d{1,32}',str(event.get('jan',''))):
            raise ValueError('JANが不正です')
        product=event['product'];master=product['master_id']
        if not re.fullmatch(r'\d{1,32}',master):raise ValueError('商品IDが不正です')
        if any(not event.get('expected',{}).get(k) for k in ('model','name')):
            raise ValueError('紐づけの型番・正式名がありません')
        encoded_image=product.pop('_image_base64',None)
        if encoded_image:
            if len(encoded_image)>60000:raise ValueError('保存画像が大きすぎます')
            raw=base64.b64decode(encoded_image,validate=True)
            with Image.open(io.BytesIO(raw)) as image:image.verify()
            filename=f'{master}-{hashlib.sha256(raw).hexdigest()[:32]}.png'
            self.data_dir.mkdir(parents=True,exist_ok=True)
            (self.data_dir/filename).write_bytes(raw)
            product['image_url']='/api/trader/image/'+filename
        else:
            # Old machine-local URLs cannot be treated as restored evidence.
            product.pop('image_url',None)
        return event

    def load_events(self):
        with self.lock:
            rows=self.sheet().get_all_values()
            events=[];seen=set()
            for number,row in enumerate(rows[1:],2):
                if not any(row):continue
                if len(row)<10:raise ValueError(f'記録シートの{number}行目が不完全です')
                event=self.unpack(row[9])
                if event['event_id']!=row[0] or event['jan']!=row[2] or event['product']['master_id']!=row[4]:
                    raise ValueError(f'記録シートの{number}行目に不整合があります')
                if event['event_id'] not in seen:
                    events.append(event);seen.add(event['event_id'])
            return events

    def append_event(self,event):
        encoded=self.pack(event)
        with self.lock:
            self.sheet().append_row([event['event_id'],event['recorded_at'],event['jan'],
                event['kind'],event['product']['master_id'],event['product'].get('observed',{}).get('name',''),
                event['product'].get('observed',{}).get('model',''),
                '' if event['product'].get('price_yen') is None else str(event['product']['price_yen']),
                event['product'].get('status',''),encoded],value_input_option='RAW')
