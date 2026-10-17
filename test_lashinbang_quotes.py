import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lashinbang_quotes import LashinbangQuotes, search_paths, lookup_quote, parse_detail, supported_jan

JAN = "4580886930067"
PATH = "/kaitori/detail/8695570"
# Minimal form of the official buyback DOM verified on 2026-10-17.
DETAIL = '''<div id="item_data"><h1>Figure</h1>
<div class="kaitori_price_box"><p class="price red">1,200<span>円</span></p></div>
<table><tr><th>JANコード</th><td>4580886930067</td></tr></table></div>'''
def search_body(items):
    return 'callback(' + json.dumps({'kotohaco': {'result': {'info': {'status': 0, 'hitnum': len(items)}, 'items': items}}}) + ')'

ITEM = {'itemid': '8695570', 'keyword2': JAN, 'number10': 1, 'price': 1690}
LIST = search_body([ITEM])


class ParsingTests(unittest.TestCase):
    def test_exact_jan_and_buyback_price(self):
        quote = parse_detail(DETAIL, JAN, PATH)
        self.assertEqual(quote["price_yen"], 1200)
        self.assertEqual(quote["source_url"], "https://shop.lashinbang.com" + PATH)
        self.assertIsNone(parse_detail(DETAIL, "4580886930074", PATH))

    def test_zero_missing_and_ambiguous_price(self):
        self.assertEqual(parse_detail(DETAIL.replace("1,200", "0"), JAN, PATH)["price_yen"], 0)
        for value in ("要確認", "1,200～2,000", "1,200円 / 2,000"):
            self.assertEqual(parse_detail(DETAIL.replace("1,200", value), JAN, PATH)["status"], "needs_review")

    def test_wrong_page_and_absent_jan_are_not_prices(self):
        with self.assertRaises(ValueError):
            parse_detail('<h1>ログイン</h1>', JAN, PATH)
        self.assertIsNone(parse_detail(DETAIL.replace("JANコード", "商品番号"), JAN, PATH))

    def test_search_is_scoped_to_buyback_and_exact_jan(self):
        self.assertEqual(search_paths(LIST, JAN), [PATH])
        self.assertEqual(search_paths(search_body([]), JAN), [])
        self.assertEqual(search_paths(search_body([{**ITEM, 'number10': 0}]), JAN), [])
        self.assertEqual(search_paths(search_body([{**ITEM, 'keyword2': '4580886930074'}]), JAN), [])
        with self.assertRaises(ValueError):
            search_paths('<h1>アクセスできません</h1>', JAN)
        with self.assertRaises(ValueError):
            search_paths(search_body([{**ITEM, 'itemid': '../123'}]), JAN)

    def test_lookup_verifies_detail_instead_of_search_position(self):
        session = Mock()
        def response(html):
            result = Mock(text=html, url=("https://lashinbang-f-s.snva.jp/" if html.startswith("callback(") else "https://shop.lashinbang.com" + PATH))
            return result
        session.get.side_effect = [response(LIST), response(DETAIL)]
        self.assertEqual(lookup_quote(JAN, session)["price_yen"], 1200)
        self.assertEqual(session.get.call_count, 2)

    def test_multiple_exact_matches_require_review(self):
        session = Mock()
        def response(html):
            return Mock(text=html, url=("https://lashinbang-f-s.snva.jp/" if html.startswith("callback(") else "https://shop.lashinbang.com" + PATH))
        session.get.side_effect = [response(search_body([ITEM, {**ITEM, 'itemid':'8695571'}])), response(DETAIL), response(DETAIL)]
        self.assertEqual(lookup_quote(JAN, session)["status"], "needs_review")

    def test_internal_codes_never_search(self):
        session = Mock()
        self.assertFalse(supported_jan("2000000000015"))
        self.assertFalse(supported_jan("../123"))
        self.assertEqual(lookup_quote("2000000000015", session)["status"], "unsupported")
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

    def test_expired_quote_schedules_refresh_and_is_marked_stale(self):
        old = {"status": "ok", "price_yen": 500, "checked_at": "2026-10-16T12:00:00+09:00"}
        with self.service.connect() as db:
            db.execute('INSERT INTO lashinbang_quotes VALUES(?,?,?)', (JAN, json.dumps(old), time.time()-1))
        with patch.object(self.service, 'schedule') as schedule:
            self.assertTrue(self.service.get_many([JAN])[JAN]['stale'])
            schedule.assert_called_once()


if __name__ == '__main__':
    unittest.main()
