"""Trader integration. Local product links and history, serial background network work."""
import datetime as dt
import hashlib
import io
import json
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from PIL import Image
from price_ocr import normalize, read_price
from trader_search import Client, URL, form_payload, parse_results, parse_detail
from search_terms import trader_query as search_query, shorter_query

TTL=6*60*60

def now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec='seconds')

def image_digest(path):
    """価格画像の同一判定用。ファイル形式の違いに左右されないよう、絵の中身（画素）から求める"""
    with Image.open(path) as image:
        return hashlib.sha256(repr(image.size).encode()+image.convert('RGB').tobytes()).hexdigest()

def suggest_query(title):
    return search_query(title)

class StartRequest(BaseModel):
    jan: str = Field(min_length=1,max_length=32,pattern=r'^\d+$')
    query: str | None = Field(default=None,min_length=1,max_length=100)
    refresh: bool = False
    search: bool = False

class ConfirmRequest(BaseModel):
    jan: str = Field(min_length=1,max_length=32,pattern=r'^\d+$')
    price_yen: int

class UnlinkRequest(BaseModel):
    jan: str = Field(min_length=1,max_length=32,pattern=r'^\d+$')

class SelectRequest(BaseModel):
    jan: str = Field(min_length=1,max_length=32,pattern=r'^\d+$')
    master_id: str = Field(min_length=1,max_length=32,pattern=r'^\d+$')

class TraderService:
    def __init__(self, db_path, data_dir):
        self.db_path=str(db_path)
        self.data_dir=Path(data_dir); self.data_dir.mkdir(parents=True,exist_ok=True)
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='trader')
        self.guard=threading.Lock(); self.jobs={}; self.active={}
        self.sheet_store=None
        self.persistence_lock=threading.RLock()
        self._restored=False
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS trader_products(master_id TEXT PRIMARY KEY,data TEXT NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS trader_links(jan TEXT PRIMARY KEY,master_id TEXT NOT NULL,expected TEXT NOT NULL,inventory_title TEXT NOT NULL,linked_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trader_history(id INTEGER PRIMARY KEY AUTOINCREMENT,jan TEXT,master_id TEXT NOT NULL,checked_at TEXT NOT NULL,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trader_searches(query TEXT PRIMARY KEY,ids TEXT NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS trader_synced_events(event_id TEXT PRIMARY KEY);
            ''')

    def connect(self):
        db=sqlite3.connect(self.db_path,timeout=30);db.row_factory=sqlite3.Row
        return db

    def item(self,jan):
        with self.connect() as db: row=db.execute('SELECT title FROM items WHERE jan=?',(jan,)).fetchone()
        if row is None: raise HTTPException(404,'JANの商品が未登録です')
        return row['title'] or ''

    def product(self,master):
        with self.connect() as db: row=db.execute('SELECT data,updated FROM trader_products WHERE master_id=?',(master,)).fetchone()
        return (json.loads(row['data']),row['updated']) if row else (None,0)

    def state(self,jan):
        self.ensure_restored()
        title=self.item(jan)
        with self.connect() as db:
            link=db.execute('SELECT * FROM trader_links WHERE jan=?',(jan,)).fetchone()
            history=db.execute('SELECT data FROM trader_history WHERE jan=? ORDER BY id DESC LIMIT 20',(jan,)).fetchall()
        product,updated=self.product(link['master_id']) if link else (None,0)
        link_data=dict(link) if link else None
        if link_data:
            link_data['expected']=json.loads(link_data['expected'])
            link_data['title_changed']=normalize(title)!=normalize(link_data['inventory_title'])
            if product and any(normalize(product.get('observed',{}).get(k,'')) != normalize(link_data['expected'].get(k,'')) for k in ('model','name')):
                product={**product,'price_yen':None,'status':'identity_mismatch','error':'保存した型番・商品名と異なります。候補を再検索してください'}
        return {'jan':jan,'title':title,'suggested_query':suggest_query(title),'link':link_data,
                'product':product,'stale':time.time()-updated>TTL,
                'history':[json.loads(r['data']) for r in history],
                'storage':'google_sheets' if self.sheet_store is not None else 'local'}

    def quote_version(self):
        with self.connect() as db:
            return db.execute('SELECT COALESCE(MAX(id),0) FROM trader_history').fetchone()[0]

    def stock_quotes(self, jans):
        """Read saved quotes only; never search or refresh the remote shop here."""
        self.ensure_restored()
        wanted=set(jans)
        result={jan:{'status':'unlinked','price_yen':None} for jan in wanted}
        with self.connect() as db:
            rows=db.execute("""
                SELECT l.jan,l.expected,l.inventory_title,i.title,p.data,p.updated
                FROM trader_links l LEFT JOIN items i ON i.jan=l.jan
                LEFT JOIN trader_products p ON p.master_id=l.master_id
            """).fetchall()
        for row in rows:
            if row['jan'] not in wanted: continue
            product=json.loads(row['data']) if row['data'] else {}
            expected=json.loads(row['expected'])
            valid=normalize(row['title'] or '')==normalize(row['inventory_title']) and all(
                normalize(product.get('observed',{}).get(k,''))==normalize(expected.get(k,''))
                for k in ('model','name'))
            price=product.get('price_yen')
            ok=valid and product.get('status')=='ok' and type(price) is int and price>=0
            quote={'status':'ok' if ok else 'needs_review','jan':row['jan'],
                'price_yen':price if ok else None,'checked_at':product.get('checked_at',''),
                'stale':time.time()-(row['updated'] or 0)>TTL}
            if ok and product.get('price_source'):
                quote['price_source']=product['price_source']
            if not ok and valid and product.get('image_url'):
                # 商品の同一性は確認済みで、価格画像だけ読み取れなかった → 画像を見て手入力できる
                quote['price_image_url']=product['image_url']
            result[row['jan']]=quote
        return result

    def event(self,kind,jan,expected,title,linked_at,product):
        return {'schema':1,'event_id':uuid.uuid4().hex,'kind':kind,'jan':jan,
                'recorded_at':now(),'expected':expected,'inventory_title':title,
                'linked_at':linked_at,'product':product}

    def ensure_restored(self):
        if self.sheet_store is None or self._restored:return
        with self.persistence_lock:
            if self._restored:return
            try:
                events=self.sheet_store.load_events()
                with self.connect() as db:
                    for event in events:
                        if db.execute('SELECT 1 FROM trader_synced_events WHERE event_id=?',(event['event_id'],)).fetchone():continue
                        product=event['product'];jan=event['jan'];master=product['master_id']
                        current=db.execute('SELECT master_id,expected FROM trader_links WHERE jan=?',(jan,)).fetchone()
                        if event['kind']=='unlink':
                            if current and current['master_id']==master:
                                db.execute('DELETE FROM trader_links WHERE jan=?',(jan,))
                            self.record_history(db,jan,product)
                            db.execute('INSERT INTO trader_synced_events VALUES(?)',(event['event_id'],))
                            continue
                        accepted=event['kind']=='select' or (current and current['master_id']==master and json.loads(current['expected'])==event['expected'])
                        if event['kind']=='select':
                            db.execute('INSERT OR REPLACE INTO trader_links VALUES(?,?,?,?,?)',
                                (jan,master,json.dumps(event['expected'],ensure_ascii=False),event['inventory_title'],event['linked_at']))
                        if accepted:
                            db.execute('INSERT OR REPLACE INTO trader_products VALUES(?,?,?)',
                                (master,json.dumps(product,ensure_ascii=False),0))
                        self.record_history(db,jan,product)
                        db.execute('INSERT INTO trader_synced_events VALUES(?)',(event['event_id'],))
                self._restored=True
            except Exception as exc:
                raise HTTPException(503,'Google Sheetsからの復元に失敗しました。共有設定・資格情報・通信を確認して再実行してください') from exc

    def persist(self,event):
        if self.sheet_store is not None:
            try:self.sheet_store.append_event(event)
            except Exception as exc:
                raise HTTPException(503,'Google Sheetsへの保存完了を確認できませんでした。通信・共有設定を確認して再実行してください') from exc

    def choose(self,jan,master):
        self.ensure_restored()
        with self.persistence_lock:
            title=self.item(jan); product,_=self.product(master)
            if not product or not product.get('observed',{}).get('model'):raise HTTPException(404,'候補を再検索してください')
            expected=product['observed'];linked_at=now()
            event=self.event('select',jan,expected,title,linked_at,product)
            self.persist(event)  # Remote acknowledgement precedes local success.
            with self.connect() as db:
                db.execute('INSERT OR REPLACE INTO trader_links VALUES(?,?,?,?,?)',
                    (jan,master,json.dumps(expected,ensure_ascii=False),title,linked_at))
                self.record_history(db,jan,product)
                db.execute('INSERT OR IGNORE INTO trader_synced_events VALUES(?)',(event['event_id'],))
        return self.state(jan)

    def confirm_price(self,jan,price):
        """価格画像を読み取れなかった商品に、画像を見た人が金額を入力して確定する"""
        if type(price) is not int or not 0<=price<=10_000_000:
            raise HTTPException(400,'金額は0〜10,000,000円で入力してください')
        self.ensure_restored()
        with self.persistence_lock:
            with self.connect() as db:
                row=db.execute('SELECT * FROM trader_links WHERE jan=?',(jan,)).fetchone()
            if row is None: raise HTTPException(404,'先にトレーダーの商品を紐づけてください')
            if normalize(self.item(jan))!=normalize(row['inventory_title']):
                raise HTTPException(409,'登録商品名が変わったため、候補を選び直してください')
            product,updated=self.product(row['master_id'])
            expected=json.loads(row['expected'])
            if not product or any(normalize(product.get('observed',{}).get(k,''))!=normalize(expected.get(k,'')) for k in ('model','name')):
                raise HTTPException(409,'保存した型番・商品名と異なります。候補を再検索してください')
            filename=(product.get('image_url') or '').removeprefix('/api/trader/image/')
            path=self.data_dir/filename
            if not re.fullmatch(r'\d+-[0-9a-f]{32}\.png',filename) or not path.is_file():
                raise HTTPException(409,'価格画像がありません。価格を更新してから入力してください')
            confirmed={**product,'status':'ok','price_yen':price,'price_source':'manual',
                'manual_confirmed_at':now(),'image_sha256':image_digest(path)}
            event=self.event('quote',jan,expected,row['inventory_title'],row['linked_at'],confirmed)
            self.persist(event)  # Remote acknowledgement precedes local success.
            with self.connect() as db:
                # 確認しただけなので取得時刻（古さの判定）はそのまま
                db.execute('INSERT OR REPLACE INTO trader_products VALUES(?,?,?)',
                    (row['master_id'],json.dumps(confirmed,ensure_ascii=False),updated))
                self.record_history(db,jan,confirmed)
                db.execute('INSERT OR IGNORE INTO trader_synced_events VALUES(?)',(event['event_id'],))
        return confirmed

    def unlink(self,jan):
        """間違えて紐づけた商品を外す。解除も記録として残し、再起動後の復元でも外れたままにする"""
        self.ensure_restored()
        with self.persistence_lock:
            with self.connect() as db:
                row=db.execute('SELECT * FROM trader_links WHERE jan=?',(jan,)).fetchone()
            if row is None: raise HTTPException(404,'この商品はトレーダーに紐づけられていません')
            product,_=self.product(row['master_id'])
            # 解除の記録に価格画像は不要（画像が無くても解除できるように外す）
            snapshot={k:v for k,v in (product or {}).items() if k not in ('image_url','payload','image_source')}
            snapshot.update(master_id=row['master_id'],checked_at=now(),status='unlinked',price_yen=None)
            snapshot.setdefault('observed',json.loads(row['expected']))
            event=self.event('unlink',jan,json.loads(row['expected']),row['inventory_title'],row['linked_at'],snapshot)
            self.persist(event)  # Remote acknowledgement precedes local success.
            with self.connect() as db:
                db.execute('DELETE FROM trader_links WHERE jan=? AND master_id=?',(jan,row['master_id']))
                self.record_history(db,jan,snapshot)
                db.execute('INSERT OR IGNORE INTO trader_synced_events VALUES(?)',(event['event_id'],))
        return self.state(jan)

    def accept_quote(self,jan,linked,current,previous,force):
        with self.persistence_lock:
            with self.connect() as db:
                row=db.execute('SELECT * FROM trader_links WHERE jan=?',(jan,)).fetchone()
            if row is None or row['master_id']!=linked['master_id'] or json.loads(row['expected'])!=linked['expected']:
                raise ValueError('取得中に紐づけが変更されました。もう一度商品を開いてください')
            event=self.event('quote',jan,linked['expected'],row['inventory_title'],row['linked_at'],current)
            try:
                if force or current['checked_at']!=previous['checked_at']:self.persist(event)
            except Exception:
                # Keep the last durable quote visible when remote persistence fails.
                with self.connect() as db:
                    db.execute('INSERT OR REPLACE INTO trader_products VALUES(?,?,?)',
                        (previous['master_id'],json.dumps(previous,ensure_ascii=False),0))
                raise
            with self.connect() as db:
                self.record_history(db,jan,current)
                db.execute('INSERT OR IGNORE INTO trader_synced_events VALUES(?)',(event['event_id'],))

    def record_history(self,db,jan,product):
        entry={k:v for k,v in product.items() if k not in ('payload','image_source')}
        entry['jan']=jan
        db.execute('INSERT INTO trader_history(jan,master_id,checked_at,data) VALUES(?,?,?,?)',
            (jan,product['master_id'],product['checked_at'],json.dumps(entry,ensure_ascii=False)))

    def start(self,request):
        state=self.state(request.jan)
        query=search_query(request.query or state['suggested_query'])
        if not query: raise HTTPException(400,'検索語を入力してください')
        linked=state['link'] if not request.search and not request.query else None
        if linked and linked['title_changed']: linked=None
        key=(request.jan,query,bool(request.refresh),linked['master_id'] if linked else '')
        with self.guard:
            existing=self.active.get(key)
            if existing and self.jobs.get(existing,{}).get('status') in ('queued','running'):
                return {'job_id':existing}
            # Expire completed jobs; active queued/running work is never removed.
            self.jobs={k:v for k,v in self.jobs.items() if v['status'] in ('queued','running') or time.time()-v['created']<3600}
            self.active={k:v for k,v in self.active.items() if v in self.jobs}
            if sum(j['status'] in ('queued','running') for j in self.jobs.values())>=32:
                raise HTTPException(429,'検索が混み合っています。少し待ってください')
            token=uuid.uuid4().hex
            self.jobs[token]={'status':'queued','created':time.time(),'jan':request.jan,'query':query,'progress':'順番待ち'}
            self.active[key]=token
        self.executor.submit(self.work,token,request,linked)
        return {'job_id':token}

    def job(self,token):
        with self.guard:
            job=self.jobs.get(token)
            if job is None: raise HTTPException(404,'検索状態が失効しました。再検索してください')
            return json.loads(json.dumps(job))

    def update(self,token,**values):
        with self.guard: self.jobs[token].update(values)

    def get_detail(self,client,candidate,refresh=False):
        cached,updated=self.product(candidate['master_id'])
        if cached and not refresh and time.time()-updated<TTL: return cached
        html=client.fetch(payload=candidate['payload']).decode('utf-8-sig')
        observed,src=parse_detail(html,candidate)
        master=candidate['master_id']
        stamp=uuid.uuid4().hex
        image_name=f'{master}-{stamp}.png'
        image_path=self.data_dir/image_name
        raw=client.image(src)
        with Image.open(io.BytesIO(raw)) as image: image.convert('RGB').save(image_path)
        record={'master_id':master,'observed':observed,'payload':candidate['payload'],
                'checked_at':now(),'source_url':URL,'image_url':f'/api/trader/image/{image_name}',
                'price_yen':None,'status':'needs_review','price_kind':'reference_buyback',
                'image_sha256':image_digest(image_path)}
        try:
            result=read_price(image_path);record.update(result)
            if result['ocr_agreement']:
                record.update(status='ok',price_yen=result['candidate_price_yen'])
        except Exception as exc: record['error']=str(exc)
        if (record['status']!='ok' and cached and cached.get('price_source')=='manual'
                and cached.get('image_sha256')==record['image_sha256'] and type(cached.get('price_yen')) is int):
            # 価格画像が手入力した時と同じ＝価格は変わっていないので、手入力の金額を引き継ぐ
            record.update(status='ok',price_yen=cached['price_yen'],price_source='manual',
                          manual_confirmed_at=cached.get('manual_confirmed_at'))
        # Archive the detail that supplied this model, name and image.
        (self.data_dir/f'{master}-{stamp}.html').write_text(html,encoding='utf-8')
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO trader_products VALUES(?,?,?)',
                (master,json.dumps(record,ensure_ascii=False),time.time()))
        return record

    def search(self,client,query,token,refresh):
        with self.connect() as db: cached=db.execute('SELECT * FROM trader_searches WHERE query=?',(query,)).fetchone()
        if cached and not refresh and time.time()-cached['updated']<TTL:
            products=[self.product(mid)[0] for mid in json.loads(cached['ids'])]
            if all(products): return products
        first=BeautifulSoup(client.fetch().decode('utf-8-sig'),'html.parser')
        form=first.select_one('form#fkensaku'); radio=first.find('input',id='1fgp')
        if form is None or radio is None: raise ValueError('検索フォームが見つかりません')
        payload=form_payload(form);payload.update(frm_keyw=query,frm_kisword=radio['value'])
        items,pages,count=parse_results(client.fetch(payload=payload).decode('utf-8-sig'))
        if count>30: raise ValueError(f'候補が{count}件あります。検索語を絞ってください（上限30件）')
        if len(items)!=count: raise ValueError('全候補を取得できません。検索語を絞ってください')
        products=[]
        for index,candidate in enumerate(items,1):
            self.update(token,progress=f'候補の価格を確認中 {index}/{count}')
            try: products.append(self.get_detail(client,candidate,refresh))
            except Exception as exc:
                products.append({'master_id':candidate['master_id'],'observed':{'name':candidate['name']},
                                 'price_yen':None,'status':'error','error':str(exc)})
        if all(p.get('observed',{}).get('model') for p in products):
            with self.connect() as db:
                db.execute('INSERT OR REPLACE INTO trader_searches VALUES(?,?,?)',
                    (query,json.dumps([p['master_id'] for p in products]),time.time()))
        return products

    def work(self,token,request,linked):
        try:
            self.update(token,status='running',progress='トレーダーを確認中')
            client=Client()
            if linked:
                product,_=self.product(linked['master_id'])
                if not product: raise ValueError('紐づけた商品の情報がありません。候補を再検索してください')
                expected=linked['expected']
                candidate={'master_id':product['master_id'],'name':expected['name'],'payload':product['payload']}
                current=self.get_detail(client,candidate,request.refresh)
                if any(normalize(current['observed'].get(k,''))!=normalize(expected.get(k,'')) for k in ('model','name')):
                    raise ValueError('保存した型番・商品名と異なります。候補を再検索してください')
                self.accept_quote(request.jan,linked,current,product,request.refresh)
                products=[current]
            else:
                query=self.job(token)['query']
                products=self.search(client,query,token,request.refresh)
                shorter=shorter_query(query)
                if not products and shorter != query:
                    self.update(token,query=shorter,progress='候補がないため検索語を短くして再検索中')
                    products=self.search(client,shorter,token,request.refresh)
            self.update(token,status='done',progress='完了',products=products,linked=bool(linked))
        except Exception as exc:
            self.update(token,status='error',progress='取得できませんでした',error=exc.detail if isinstance(exc,HTTPException) else str(exc))


def build_router(service):
    router=APIRouter(prefix='/api/trader')
    @router.get('/state/{jan}')
    def state(jan: str): return service.state(jan)
    @router.post('/start')
    def start(request: StartRequest): return service.start(request)
    @router.get('/jobs/{token}')
    def job(token: str): return service.job(token)
    @router.post('/confirm')
    def confirm(request: ConfirmRequest): return service.confirm_price(request.jan,request.price_yen)
    @router.post('/unlink')
    def unlink(request: UnlinkRequest): return service.unlink(request.jan)
    @router.post('/select')
    def choose(request: SelectRequest): return service.choose(request.jan,request.master_id)
    @router.get('/image/{filename}')
    def image(filename: str):
        if not re.fullmatch(r'\d+-[0-9a-f]{32}\.png',filename): raise HTTPException(404)
        path=service.data_dir/filename
        if not path.is_file(): raise HTTPException(404)
        return FileResponse(path,media_type='image/png')
    return router

