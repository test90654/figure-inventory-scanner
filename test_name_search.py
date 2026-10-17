import ast
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from search_terms import search_query, shorter_query, trader_query
from lashinbang_quotes import LashinbangQuotes, search_quotes

CODE = '2000000000015'
CANDIDATE = {'master_id': '7892986', 'remote_jan': '4573102703118',
             'title': 'NARUTO はたけカカシ', 'status': 'ok', 'price_yen': 600}


class SearchTermsTests(unittest.TestCase):
    def test_punctuation_is_spacing_and_edition_words_survive(self):
        self.assertEqual(search_query('【限定】NARUTO－ナルト－（カカシ） ～AMP+～ A賞'),
                         '限定 NARUTO ナルト カカシ AMP+ A賞')
        self.assertEqual(search_query('A‐B‑C–D—E−F'), 'A B C D E F')
        self.assertEqual(shorter_query('BANDAI SPIRITS NARUTO フィギュア カカシ PLUS 限定 A賞'),
                         'NARUTO カカシ PLUS 限定 A賞')
        self.assertEqual(shorter_query('フィギュア カカシ'), 'フィギュア カカシ')

    def test_trader_removes_full_and_half_width_long_marks(self):
        self.assertEqual(trader_query('ーNARUTOーナルトｰ カカシ 限定 PLUS A賞'),
                         'NARUTO ナルト カカシ 限定 PLUS A賞')
        self.assertEqual(trader_query('ｰフィギュアｰ'), 'フィギュア')
        self.assertEqual(search_query('セーラームーン'), 'セーラームーン')

    def test_trader_retries_shorter_query_only_after_empty_result(self):
        # Exercise the actual worker without importing FastAPI/OCR at module scope.
        tree = ast.parse(Path(__file__).with_name('trader_integration.py').read_text(encoding='utf-8'))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'TraderService')
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'work')
        namespace = {'Client': Mock(), 'shorter_query': shorter_query, 'HTTPException': type('HttpError', (Exception,), {})}
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'trader_integration.py', 'exec'), namespace)
        service = Mock()
        service.job.return_value = {'query': 'NARUTO フィギュア カカシ PLUS'}
        service.search.side_effect = [[], [CANDIDATE]]
        request = Mock(refresh=False)
        namespace['work'](service, 'token', request, None)
        self.assertEqual([call.args[1] for call in service.search.call_args_list],
                         ['NARUTO フィギュア カカシ PLUS', 'NARUTO カカシ PLUS'])
        self.assertEqual(service.update.call_args.kwargs['status'], 'done')

    def test_name_search_does_not_require_jan_in_remote_record(self):
        body = 'callback(' + json.dumps({'kotohaco': {'result': {'info': {'status': 0, 'hitnum': 1},
                'items': [{'itemid': '123', 'title': 'No barcode', 'keyword2': '', 'number10': 1,
                           'number5': 500, 'number19': 600, 'price': 9000}]}}}) + ')'
        candidate = search_quotes(body, max_results=30, allow_partial=True)[0]
        self.assertEqual(candidate['master_id'], '123')
        self.assertEqual(candidate['price_yen'], 600)


class InventoryCodeTests(unittest.TestCase):
    def test_registered_18_digit_code_is_accepted_but_unknown_code_is_not(self):
        tree = ast.parse(Path(__file__).with_name('inventory_browser_scan.py').read_text(encoding='utf-8'))
        method = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'validate_lashinbang_item')
        class HttpError(Exception):
            def __init__(self, status, detail):
                self.status_code = status
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / 'stock.db')
            db = sqlite3.connect(db_path)
            db.execute('CREATE TABLE stock(jan TEXT,count INTEGER)')
            db.execute('INSERT INTO stock VALUES(?,?)', ('123456789012345678', 2))
            db.commit()
            db.close()
            namespace = {'re': re, 'sqlite3': sqlite3, 'db_path': db_path, 'HTTPException': HttpError}
            exec(compile(ast.Module(body=[method], type_ignores=[]), 'inventory_browser_scan.py', 'exec'), namespace)
            namespace['validate_lashinbang_item']('123456789012345678')
            with self.assertRaises(HttpError) as unknown:
                namespace['validate_lashinbang_item']('999999999999999999')
            self.assertEqual(unknown.exception.status_code, 404)
            with self.assertRaises(HttpError) as invalid:
                namespace['validate_lashinbang_item']('not-a-code')
            self.assertEqual(invalid.exception.status_code, 400)


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = LashinbangQuotes(Path(self.tmp.name) / 'db.sqlite')
        with self.service.connect() as db:
            db.execute('CREATE TABLE items(jan TEXT PRIMARY KEY,title TEXT)')
            db.execute('INSERT INTO items VALUES(?,?)', (CODE, 'Inventory Kakashi'))

    def tearDown(self):
        self.service.executor.shutdown(wait=True)
        self.tmp.cleanup()

    def search(self):
        with patch('lashinbang_quotes.catalog_search', return_value=[CANDIDATE]):
            return self.service.name_candidates(CODE, 'NARUTO-カカシ')

    def test_only_search_candidates_can_be_selected(self):
        search = self.search()
        self.assertIsNone(self.service.link(CODE))
        with self.assertRaises(ValueError):
            self.service.select(CODE, search['token'], '999')
        with self.assertRaises(ValueError):
            self.service.select('2000000000022', search['token'], CANDIDATE['master_id'])
        selected = self.service.select(CODE, search['token'], CANDIDATE['master_id'])
        self.assertEqual(selected['price_yen'], 600)
        self.assertEqual(self.service.get_many([CODE])[CODE]['linked'], True)

    def test_durable_save_failure_does_not_commit_local_link(self):
        search = self.search()
        store = Mock()
        store.load.return_value = {}
        store.save.side_effect = RuntimeError('Sheets unavailable')
        self.service.link_store = store
        with self.assertRaises(RuntimeError):
            self.service.select(CODE, search['token'], CANDIDATE['master_id'])
        self.assertIsNone(self.service.link(CODE))
        self.assertIsNone(self.service.cached(CODE)[0])

    def test_link_restoration_and_refresh_use_selected_id(self):
        search = self.search()
        self.service.select(CODE, search['token'], CANDIDATE['master_id'])
        link = self.service.link(CODE)
        with self.service.connect() as db:
            db.execute('DELETE FROM lashinbang_links')
        store = Mock()
        store.load.return_value = {CODE: link}
        self.service.link_store = store
        restored = self.service.link(CODE)
        with patch('lashinbang_quotes.catalog_search', return_value=[{**CANDIDATE, 'master_id': '123', 'price_yen': 9999}, CANDIDATE]):
            self.assertEqual(self.service.linked_quote(restored)['price_yen'], 600)
        with patch('lashinbang_quotes.catalog_search', return_value=[{**CANDIDATE, 'master_id': '123'}]):
            self.assertIsNone(self.service.linked_quote(restored)['price_yen'])
        with self.service.connect() as db:
            db.execute('UPDATE items SET title=?', ('Different character',))
        self.assertEqual(self.service.get_many([CODE])[CODE]['status'], 'needs_review')
        self.assertIsNone(self.service.get_many([CODE])[CODE]['price_yen'])

    def test_renamed_inventory_and_expired_search_need_new_confirmation(self):
        search = self.search()
        with self.service.connect() as db:
            db.execute('UPDATE items SET title=?', ('Changed',))
        with self.assertRaises(ValueError):
            self.service.select(CODE, search['token'], CANDIDATE['master_id'])
        with self.service.guard:
            self.service.searches[search['token']]['created'] = 0
        with self.assertRaises(ValueError):
            self.service.select(CODE, search['token'], CANDIDATE['master_id'])


class SheetStoreTests(unittest.TestCase):
    def test_confirmed_link_round_trip_and_raw_storage(self):
        from lashinbang_link_store import LinkSheetStore, HEADERS
        sheet = Mock()
        sheet.row_values.return_value = HEADERS
        provider = Mock()
        provider.return_value.worksheet.return_value = sheet
        store = LinkSheetStore(provider)
        link = {'schema': 1, 'code': CODE, 'master_id': '7892986',
                'inventory_title': 'Inventory Kakashi', 'title': CANDIDATE['title'],
                'remote_jan': '4573102703118', 'linked_at': '2026-10-18T00:00:00+09:00'}
        store.save(link)
        sheet.append_row.assert_called_once()
        self.assertEqual(sheet.append_row.call_args.kwargs['value_input_option'], 'RAW')
        row = sheet.append_row.call_args.args[0]
        sheet.get_all_values.return_value = [HEADERS, row]
        self.assertEqual(store.load(), {CODE: link})

    def test_incompatible_sheet_header_is_not_overwritten(self):
        from lashinbang_link_store import LinkSheetStore
        sheet = Mock()
        sheet.row_values.return_value = ['Existing data']
        provider = Mock()
        provider.return_value.worksheet.return_value = sheet
        store = LinkSheetStore(provider)
        with self.assertRaises(ValueError):
            store.load()
        sheet.update.assert_not_called()


if __name__ == '__main__':
    unittest.main()
