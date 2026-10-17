"""Append-only confirmed links; quotes themselves remain a disposable cache."""
import json
import threading
import gspread

HEADERS = ['記録日時', '在庫コード', '商品ID', '在庫商品名', 'データJSON']


class LinkSheetStore:
    def __init__(self, provider):
        self.provider = provider
        self.guard = threading.Lock()
        self.worksheet = None

    def sheet(self):
        if self.worksheet is None:
            book = self.provider()
            try:
                sheet = book.worksheet('らしんばん紐づけ')
            except gspread.exceptions.WorksheetNotFound:
                sheet = book.add_worksheet(title='らしんばん紐づけ', rows=1000, cols=5)
            header = sheet.row_values(1)
            if not header:
                sheet.update(range_name='A1:E1', values=[HEADERS], value_input_option='RAW')
            elif header != HEADERS:
                raise ValueError('らしんばん紐づけシートの見出しが異なります')
            self.worksheet = sheet
        return self.worksheet

    def load(self):
        with self.guard:
            rows = self.sheet().get_all_values()
        links = {}
        for row in rows[1:]:
            if not any(row):
                continue
            if len(row) < 5:
                raise ValueError('らしんばん紐づけの保存データが不完全です')
            link = json.loads(row[4])
            if link.get('schema') != 1 or link.get('code') != row[1] or link.get('master_id') != row[2]:
                raise ValueError('らしんばん紐づけの保存データに不整合があります')
            if not str(link['code']).isdigit() or not str(link['master_id']).isdigit():
                raise ValueError('らしんばん紐づけのコードが不正です')
            links[link['code']] = link
        return links

    def save(self, link):
        with self.guard:
            self.sheet().append_row([link['linked_at'], link['code'], link['master_id'],
                                     link['inventory_title'], json.dumps(link, ensure_ascii=False)],
                                    value_input_option='RAW')
