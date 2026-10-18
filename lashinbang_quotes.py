"""JAN-verified, read-only Lashinbang buyback quotes with a disposable cache."""
import datetime as dt
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from urllib.parse import urlparse

import requests
from search_terms import search_query

BASE = "https://shop.lashinbang.com"
TTL = 6 * 3600
ERROR_TTL = 5 * 60
# 1件の取得がこれ以上終わらなければ作業スレッドが固まったとみなし、作り直して残りを続ける
STUCK_AFTER = 120
# 紐づけ(Google Sheets)の読み込みを他のスレッドが待つ上限
LINK_LOAD_WAIT = 30
_UNKNOWN = object()
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": BASE + "/kaitori",
}


def supported_jan(jan):
    return bool(re.fullmatch(r"[0-9]{13}", jan)) and not jan.startswith("20")


def search_quotes(body, jan=None, max_results=5, allow_partial=False):
    # Same JSONP search used by the official kaitori page; never execute it.
    match = re.fullmatch(r"\s*callback\s*\((.*)\);?\s*", body, re.DOTALL)
    if match is None:
        raise ValueError("Unrecognized buyback search response")
    result = json.loads(match.group(1))["kotohaco"]["result"]
    info, items = result["info"], result["items"]
    if info.get("status") != 0 or not isinstance(items, list):
        raise ValueError("Buyback search failed")
    count = info["hitnum"]
    if type(count) is not int or count < 0:
        raise ValueError("Invalid buyback result count")
    if count > max_results and not allow_partial:
        return None
    if len(items) != min(count, max_results):
        raise ValueError("Incomplete buyback results")
    quotes = []
    seen = set()
    for item in items:
        # number10=1 is the standard buyback product, excluding retail stock.
        if (jan is not None and item.get("keyword2", "").strip() != jan) or item.get("number10") != 1:
            continue
        product_id = str(item.get("itemid", ""))
        if not re.fullmatch(r"[0-9]+", product_id):
            raise ValueError("Invalid buyback product ID")
        if product_id in seen:
            continue
        seen.add(product_id)
        # The official buyback list displays max(number5, number19).
        # `price` is the retail sale price and must never be used here.
        values = [item.get(key) for key in ("number5", "number19") if item.get(key) is not None]
        valid = bool(values) and all(type(value) is int and value >= 0 for value in values)
        quotes.append({"status": "ok" if valid else "needs_review",
                       "price_yen": max(values) if valid else None,
                       "title": item.get("title", ""), "master_id": product_id,
                       "remote_jan": item.get("keyword2", "").strip(),
                       "image_url": item.get("image", "") if urlparse(item.get("image", "")).scheme == "https" and urlparse(item.get("image", "")).netloc in ("img.lashinbang.com", "devimg.lashinbang.com") else "",
                       "source_url": BASE + "/kaitori/detail/" + product_id})
    return quotes


def lookup_quote(jan, session=None):
    if not supported_jan(jan):
        return {"status": "unsupported", "price_yen": None}
    def fetch(url, params=None):
        response = client.get(url, params=params, timeout=(5, 15))
        response.raise_for_status()
        actual = urlparse(response.url)
        if actual.scheme != "https" or actual.netloc != urlparse(url).netloc:
            raise ValueError("Unexpected buyback redirect")
        response.encoding = "utf-8"
        return response.text

    own_session = session is None
    client = session or requests.Session()
    client.headers.update(HEADERS)
    try:
        params = {"k2": jan, "s1": "2", "n10c": "1:1", "n5l": "1",
                  "searchbox": "1", "limit": "6", "o": "0", "sort": "Score,Number18",
                  "controller": "lashinbang_kaitori", "callback": "callback"}
        matches = search_quotes(fetch("https://lashinbang-f-s.snva.jp/", params), jan)
        if matches is None:
            return {"status": "needs_review", "price_yen": None}
        if len(matches) == 1:
            return matches[0]
        if matches and all(quote["status"] == "ok" for quote in matches):
            prices = [quote["price_yen"] for quote in matches]
            return {"status": "multiple", "price_yen": None,
                    "price_min": min(prices), "price_max": max(prices), "candidates": matches}
        return {"status": "needs_review" if matches else "not_found", "price_yen": None}
    finally:
        if own_session:
            client.close()


def catalog_search(query=None, jan=None):
    params = {"s1": "2", "n10c": "1:1", "n5l": "1", "limit": "30", "o": "0",
              "sort": "Score,Number18", "controller": "lashinbang_kaitori", "callback": "callback"}
    if jan:
        params["k2"] = jan
    else:
        params["q"] = search_query(query)
        if not params["q"]:
            raise ValueError("検索語を入力してください")
    response = requests.get("https://lashinbang-f-s.snva.jp/", params=params, headers=HEADERS, timeout=(5, 15))
    response.raise_for_status()
    if urlparse(response.url).netloc != "lashinbang-f-s.snva.jp":
        raise ValueError("検索先が変わりました")
    response.encoding = "utf-8"
    return search_quotes(response.text, jan=jan, max_results=30, allow_partial=True)


class LashinbangQuotes:
    def __init__(self, db_path, lookup=lookup_quote):
        self.db_path = str(db_path)
        self.lookup = lookup
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lashinbang")
        self.guard = threading.Lock()
        self.pending = set()
        self.version = 0
        self.next_request = 0
        self.link_store = None
        self.links_restored = False
        self.link_guard = threading.RLock()
        self.link_load_guard = threading.Lock()
        self.executor_gen = 0
        self.active = None  # (executor_gen, jan, 開始時刻) 実行中の取得
        self.searches = {}
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS lashinbang_quotes(jan TEXT PRIMARY KEY, data TEXT NOT NULL, retry_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS lashinbang_links(code TEXT PRIMARY KEY, data TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.db_path, timeout=30)) as db:
            with db:
                yield db

    def ensure_links(self):
        if self.link_store is None or self.links_restored:
            return
        # Sheetsの読み込みは link_guard の外で行い、他の処理を巻き込んで止めない
        if not self.link_load_guard.acquire(timeout=LINK_LOAD_WAIT):
            raise TimeoutError("らしんばん紐づけの読み込みが終わっていません")
        try:
            if self.links_restored:
                return
            links = self.link_store.load()
            with self.connect() as db:
                for code, link in links.items():
                    db.execute("INSERT OR REPLACE INTO lashinbang_links VALUES(?,?)", (code, json.dumps(link, ensure_ascii=False)))
            self.links_restored = True
        finally:
            self.link_load_guard.release()

    def link(self, code):
        self.ensure_links()
        return self.local_link(code)

    def local_link(self, code):
        with self.connect() as db:
            row = db.execute("SELECT data FROM lashinbang_links WHERE code=?", (code,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def link_signature(link):
        return (link.get("master_id"), link.get("linked_at")) if link else None

    def inventory_title(self, code):
        with self.connect() as db:
            row = db.execute("SELECT title FROM items WHERE jan=?", (code,)).fetchone()
        if row is None:
            raise ValueError("在庫商品が見つかりません")
        return row[0] or ""

    def name_candidates(self, code, query):
        title = self.inventory_title(code)
        query = search_query(query or title)
        candidates = catalog_search(query=query)
        token = uuid.uuid4().hex
        with self.guard:
            self.searches = {key: value for key, value in self.searches.items() if time.time()-value['created'] < 900}
            if len(self.searches) >= 100:
                raise ValueError("検索が混み合っています。少し待ってください")
            self.searches[token] = {'created': time.time(), 'code': code, 'title': title, 'query': query, 'candidates': candidates}
        return {'token': token, 'query': query, 'candidates': candidates, 'has_more': len(candidates) == 30,
                'storage': 'google_sheets' if self.link_store is not None else 'local'}

    def select(self, code, token, master_id):
        with self.guard:
            search = self.searches.get(token)
        if not search or search['code'] != code or time.time()-search['created'] >= 900:
            raise ValueError("候補が失効しました。再検索してください")
        candidate = next((item for item in search['candidates'] if item['master_id'] == master_id), None)
        if candidate is None or not candidate.get('title'):
            raise ValueError("検索結果から商品を選んでください")
        with self.link_guard:
            if self.inventory_title(code) != search['title']:
                raise ValueError("在庫商品名が変わりました。再検索してください")
            self.ensure_links()
            stamp = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds")
            link = {'schema': 1, 'code': code, 'master_id': master_id, 'title': candidate['title'],
                    'remote_jan': candidate.get('remote_jan', ''), 'inventory_title': search['title'],
                    'query': search['query'], 'linked_at': stamp}
            if self.link_store is not None:
                self.link_store.save(link)  # Acknowledge durable storage before local success.
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO lashinbang_links VALUES(?,?)", (code, json.dumps(link, ensure_ascii=False)))
                quote = {**candidate, 'checked_at': stamp, 'linked': True}
                db.execute("INSERT OR REPLACE INTO lashinbang_quotes VALUES(?,?,?)", (code, json.dumps(quote, ensure_ascii=False), time.time()+TTL))
            with self.guard:
                self.version += 1
        return quote

    def linked_quote(self, link):
        if self.inventory_title(link['code']) != link['inventory_title']:
            return {'status': 'needs_review', 'price_yen': None, 'linked': True}
        remote_jan = link.get('remote_jan', '')
        candidates = catalog_search(jan=remote_jan) if supported_jan(remote_jan) else catalog_search(query=link['title'])
        candidate = next((item for item in candidates if item['master_id'] == link['master_id']), None)
        if candidate is None or search_query(candidate['title']) != search_query(link['title']):
            return {'status': 'needs_review', 'price_yen': None, 'linked': True}
        return {**candidate, 'linked': True}

    def cached(self, jan):
        with self.connect() as db:
            row = db.execute("SELECT data,retry_at FROM lashinbang_quotes WHERE jan=?", (jan,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else (None, 0)

    def get_many(self, jans):
        # 先に固まりを解消しておく（中断した商品が同じ一覧取得の中ですぐ再投入されないように）
        self.recover_if_stuck()
        wanted = set(jans)
        if wanted:
            self.ensure_links()
        with self.connect() as db:
            cached = {row[0]: (json.loads(row[1]), row[2]) for row in db.execute("SELECT jan,data,retry_at FROM lashinbang_quotes") if row[0] in wanted}
        result = {}
        for jan in wanted:
            link = self.link(jan)
            if link and self.inventory_title(jan) != link["inventory_title"]:
                result[jan] = {"status": "needs_review", "price_yen": None, "linked": True}
                continue
            if not supported_jan(jan) and not link:
                result[jan] = {"status": "unlinked", "price_yen": None}
                continue
            quote, retry_at = cached.get(jan, (None, 0))
            self.schedule(jan, cached_retry_at=retry_at)
            with self.guard:
                pending = jan in self.pending
            result[jan] = {**(quote or {"status": "pending", "price_yen": None}), "pending": pending, "linked": bool(link),
                           "stale": bool(quote and (quote.get("stale") or time.time() >= retry_at))}
        return result

    def schedule(self, jan, force=False, cached_retry_at=None):
        self.recover_if_stuck()
        if not supported_jan(jan) and not self.link(jan):
            return False
        retry_at = self.cached(jan)[1] if cached_retry_at is None else cached_retry_at
        if not force and time.time() < retry_at:
            return False
        with self.guard:
            if jan in self.pending:
                return False
            self.pending.add(jan)
            executor, gen = self.executor, self.executor_gen
        try:
            executor.submit(self.refresh, jan, gen)
        except Exception:
            with self.guard:
                self.pending.discard(jan)
            raise
        return True

    def recover_if_stuck(self):
        """取得が STUCK_AFTER 秒以上終わらない時、作業スレッドを作り直して残りの取得を続ける"""
        with self.guard:
            active = self.active
            if (not active or active[0] != self.executor_gen
                    or time.monotonic() - active[2] < STUCK_AFTER):
                return False
            old = self.executor
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lashinbang")
            self.executor_gen += 1
            self.active = None
            # 待ち行列ごと捨てる。次の一覧取得で未取得分が新しいスレッドへ入れ直される
            self.pending.clear()
        jan = active[1]
        logging.getLogger(__name__).warning(
            "Lashinbang lookup for %s did not finish in %ss; restarting worker", jan, STUCK_AFTER)
        old.shutdown(wait=False, cancel_futures=True)
        # 固まった商品はすぐに再投入せず、通常の失敗と同じく少し待ってから再取得する
        old_quote, _ = self.cached(jan)
        quote = ({**old_quote, "stale": True, "refresh_error": True} if old_quote
                 else {"status": "error", "price_yen": None})
        quote["error_message"] = "応答がないため取得を中断しました"
        with self.link_guard:
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO lashinbang_quotes VALUES(?,?,?)",
                           (jan, json.dumps(quote, ensure_ascii=False), time.time() + ERROR_TTL))
        with self.guard:
            self.version += 1
        return True

    def refresh(self, jan, gen=None):
        with self.guard:
            gen = self.executor_gen if gen is None else gen
            self.active = (gen, jan, time.monotonic())
        try:
            self._refresh(jan)
        finally:
            self.next_request = time.monotonic() + 1
            with self.guard:
                if self.active and self.active[0] == gen:
                    self.active = None
                # 作り直し前の古いスレッドが後から終わっても、新しい待ち行列は乱さない
                if gen == self.executor_gen:
                    self.pending.discard(jan)
                self.version += 1

    def _refresh(self, jan):
        time.sleep(max(0, self.next_request - time.monotonic()))
        before = _UNKNOWN
        try:
            # 通信はロックの外で行う（Sheetsや相手サイトが固まっても紐づけ操作を止めない）
            link = self.link(jan)
            before = self.link_signature(link)
            quote = self.linked_quote(link) if link else self.lookup(jan)
            quote["checked_at"] = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds")
            quote["stale"] = False
            retry_at = time.time() + TTL
        except Exception as exc:
            logging.getLogger(__name__).exception("Lashinbang buyback lookup failed")
            old, _ = self.cached(jan)
            quote = {**old, "stale": True, "refresh_error": True} if old else {"status": "error", "price_yen": None}
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                message = f"接続先がHTTP {exc.response.status_code}を返しました"
            elif isinstance(exc, requests.Timeout):
                message = "接続がタイムアウトしました"
            elif isinstance(exc, requests.ConnectionError):
                message = "接続できませんでした"
            elif isinstance(exc, (ValueError, KeyError, TypeError)):
                message = "検索データを読み取れませんでした"
            else:
                message = "価格を取得できませんでした"
            quote["error_message"] = message
            retry_at = time.time() + ERROR_TTL
        # 保存だけを select() と排他にし、取得中に紐づけ直された場合は古い結果を捨てる
        with self.link_guard:
            if before is not _UNKNOWN and self.link_signature(self.local_link(jan)) != before:
                return
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO lashinbang_quotes VALUES(?,?,?)", (jan, json.dumps(quote, ensure_ascii=False), retry_at))
