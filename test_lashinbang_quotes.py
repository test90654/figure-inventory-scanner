import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lashinbang_quotes import LashinbangQuotes, search_quotes, lookup_quote, supported_jan

JAN = "4580886930067"
PATH = "/kaitori/detail/8695570"
def search_body(items):
    return 'callback(' + json.dumps({'kotohaco': {'result': {'info': {'status': 0, 'hitnum': len(items)}, 'items': items}}}) + ')'

ITEM = {'itemid': '8695570', 'keyword2': JAN, 'number10': 1, 'price': 1690, 'number5': 500, 'number19': 600, 'title': 'Figure'}
LIST = search_body([ITEM])


class ParsingTests(unittest.TestCase):
    def test_uses_buyback_fields_and_never_retail_price(self):
        quote = search_quotes(LIST, JAN)[0]
        self.assertEqual(quote['price_yen'], 600)
        self.assertEqual(quote['source_url'], 'https://shop.lashinbang.com' + PATH)
        self.assertEqual(search_quotes(search_body([{**ITEM, 'number19': None}]), JAN)[0]['price_yen'], 500)

    def test_search_is_scoped_to_buyback_and_exact_jan(self):
        self.assertEqual(search_quotes(search_body([]), JAN), [])
        self.assertEqual(search_quotes(search_body([{**ITEM, 'number10': 0}]), JAN), [])
        self.assertEqual(search_quotes(search_body([{**ITEM, 'keyword2': '4580886930074'}]), JAN), [])
        with self.assertRaises(ValueError):
            search_quotes('<h1>アクセスできません</h1>', JAN)
        with self.assertRaises(ValueError):
            search_quotes(search_body([{**ITEM, 'itemid': '../123'}]), JAN)

    def test_missing_or_invalid_buyback_fields_do_not_use_sale_price(self):
        for value in (None, '500', True, -1):
            quote = search_quotes(search_body([{**ITEM, 'number5': value, 'number19': None}]), JAN)[0]
            self.assertEqual(quote['status'], 'needs_review')
            self.assertIsNone(quote['price_yen'])
        self.assertEqual(search_quotes(search_body([{**ITEM, 'number5': 0, 'number19': None}]), JAN)[0]['price_yen'], 0)

    def test_lookup_never_requests_detail_page_that_can_return_403(self):
        session = Mock()
        response = Mock(text=LIST, url='https://lashinbang-f-s.snva.jp/')
        session.get.side_effect = [response, AssertionError('Detail request must not occur')]
        self.assertEqual(lookup_quote(JAN, session)['price_yen'], 600)
        session.get.assert_called_once()
        self.assertEqual(session.get.call_args.args[0], 'https://lashinbang-f-s.snva.jp/')
        self.assertEqual(session.get.call_args.kwargs['params']['controller'], 'lashinbang_kaitori')

    def test_multiple_exact_matches_return_range_without_selecting_item(self):
        session = Mock()
        body = search_body([{**ITEM, 'number19': 500}, {**ITEM, 'itemid':'7892986', 'number19': 600}])
        session.get.return_value = Mock(text=body, url='https://lashinbang-f-s.snva.jp/')
        quote = lookup_quote(JAN, session)
        self.assertEqual(quote['status'], 'multiple')
        self.assertEqual((quote['price_min'], quote['price_max']), (500, 600))
        self.assertIsNone(quote['price_yen'])
        self.assertEqual(len(quote['candidates']), 2)
        session.get.assert_called_once()

    def test_internal_codes_never_search(self):
        session = Mock()
        self.assertFalse(supported_jan('2000000000015'))
        self.assertFalse(supported_jan('../123'))
        self.assertEqual(lookup_quote('2000000000015', session)['status'], 'unsupported')
        session.get.assert_not_called()


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lookup = Mock(return_value={"status": "ok", "price_yen": 500})
        self.service = LashinbangQuotes(Path(self.tmp.name) / 'quotes.db', self.lookup)
        self.sleep = patch('lashinbang_quotes.time.sleep').start()

    def tearDown(self):
        self.service.executor.shutdown(wait=True)
        patch.stopall()
        self.tmp.cleanup()

    def test_cache_survives_reload_and_skips_fresh_quotes(self):
        self.service.refresh(JAN)
        quote = self.service.get_many([JAN, JAN])[JAN]
        self.assertEqual(quote["price_yen"], 500)
        self.assertFalse(quote["pending"])
        self.assertFalse(quote["stale"])
        self.lookup.assert_called_once()
        other = LashinbangQuotes(self.service.db_path, self.lookup)
        try:
            self.assertEqual(other.get_many([JAN])[JAN]["price_yen"], 500)
            self.lookup.assert_called_once()
        finally:
            other.executor.shutdown(wait=True)

    def test_failure_retains_last_quote_and_sets_retry_backoff(self):
        self.service.refresh(JAN)
        self.lookup.side_effect = RuntimeError('offline')
        with self.assertLogs('lashinbang_quotes', level='ERROR'):
            self.service.refresh(JAN)
        quote = self.service.get_many([JAN])[JAN]
        self.assertEqual(quote["price_yen"], 500)
        self.assertTrue(quote["stale"])
        self.assertTrue(quote["refresh_error"])
        self.assertFalse(quote["pending"])
        self.assertEqual(self.lookup.call_count, 2)

    def test_missing_quote_failure_is_not_no_listing(self):
        self.lookup.side_effect = RuntimeError('offline')
        with self.assertLogs('lashinbang_quotes', level='ERROR'):
            self.service.refresh(JAN)
        self.assertEqual(self.service.get_many([JAN])[JAN]["status"], 'error')

    def test_background_dedup_and_completion_version(self):
        started, finish = threading.Event(), threading.Event()
        def lookup(jan):
            started.set()
            finish.wait(timeout=3)
            return {"status": "not_found", "price_yen": None}
        self.service.lookup = lookup
        try:
            self.assertTrue(self.service.schedule(JAN))
            self.assertTrue(started.wait(timeout=3))
            self.assertFalse(self.service.schedule(JAN, force=True))
            self.assertTrue(self.service.get_many([JAN])[JAN]["pending"])
        finally:
            finish.set()
            self.service.executor.shutdown(wait=True)
        self.assertEqual(self.service.version, 1)
        self.assertEqual(self.service.get_many([JAN])[JAN]["status"], 'not_found')

    def test_hung_lookup_does_not_stall_remaining_items(self):
        # 1件が応答しないまま固まっても、残りの商品の取得は続く
        other = "4983164873351"
        hang = threading.Event()
        def lookup(jan):
            if jan == JAN:
                hang.wait(timeout=5)
                return {"status": "ok", "price_yen": 1}
            return {"status": "ok", "price_yen": 700}
        self.service.lookup = lookup
        stuck_executor = self.service.executor
        try:
            with patch('lashinbang_quotes.STUCK_AFTER', 0.2):
                self.service.schedule(JAN)
                threading.Event().wait(0.4)  # time.sleep は setUp でモック済み
                with self.assertLogs('lashinbang_quotes', level='WARNING'):
                    self.service.get_many([JAN, other])
                for _ in range(50):
                    if self.service.get_many([other])[other].get("price_yen") == 700:
                        break
                    threading.Event().wait(0.1)
            self.assertEqual(self.service.get_many([other])[other]["price_yen"], 700)
            stuck = self.service.get_many([JAN])[JAN]
            self.assertEqual(stuck["status"], "error")
            self.assertFalse(stuck["pending"])
        finally:
            hang.set()
            stuck_executor.shutdown(wait=True)

    def test_network_lookup_runs_without_holding_link_lock(self):
        # 価格取得の通信中も、紐づけ操作（select）がロック待ちで止まらない
        started, finish = threading.Event(), threading.Event()
        def lookup(jan):
            started.set()
            finish.wait(timeout=3)
            return {"status": "ok", "price_yen": 500}
        self.service.lookup = lookup
        try:
            self.service.schedule(JAN)
            self.assertTrue(started.wait(timeout=3))
            acquired = []
            def probe():
                ok = self.service.link_guard.acquire(timeout=1)
                acquired.append(ok)
                if ok:
                    self.service.link_guard.release()
            thread = threading.Thread(target=probe)
            thread.start(); thread.join()
            self.assertEqual(acquired, [True])
        finally:
            finish.set()

    def test_result_fetched_before_relink_does_not_overwrite_new_link(self):
        old_link = {'code': JAN, 'master_id': '1', 'linked_at': 'a', 'inventory_title': 'X', 'title': 'X'}
        new_link = {**old_link, 'master_id': '2', 'linked_at': 'b'}
        new_quote = {"status": "ok", "price_yen": 900, "linked": True}
        with self.service.connect() as db:
            db.execute('INSERT INTO lashinbang_links VALUES(?,?)', (JAN, json.dumps(old_link)))
        def linked_quote(link):
            # 取得中にユーザーが別商品へ紐づけ直した
            with self.service.connect() as db:
                db.execute('INSERT OR REPLACE INTO lashinbang_links VALUES(?,?)', (JAN, json.dumps(new_link)))
                db.execute('INSERT OR REPLACE INTO lashinbang_quotes VALUES(?,?,?)', (JAN, json.dumps(new_quote), time.time()+999))
            return {"status": "ok", "price_yen": 100, "linked": True}
        with patch.object(self.service, 'linked_quote', side_effect=linked_quote):
            self.service.refresh(JAN)
        self.assertEqual(self.service.cached(JAN)[0]["price_yen"], 900)

    def test_expired_quote_schedules_refresh_and_is_marked_stale(self):
        old = {"status": "ok", "price_yen": 500, "checked_at": "2026-10-16T12:00:00+09:00"}
        with self.service.connect() as db:
            db.execute('INSERT INTO lashinbang_quotes VALUES(?,?,?)', (JAN, json.dumps(old), time.time()-1))
        with patch.object(self.service, 'schedule') as schedule:
            self.assertTrue(self.service.get_many([JAN])[JAN]['stale'])
            schedule.assert_called_once()


if __name__ == '__main__':
    unittest.main()
