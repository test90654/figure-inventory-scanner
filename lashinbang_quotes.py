"""JAN-verified, read-only Lashinbang buyback quotes with a disposable cache."""
import datetime as dt
import json
import logging
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from urllib.parse import urlparse

import requests

BASE = "https://shop.lashinbang.com"
TTL = 6 * 3600
ERROR_TTL = 5 * 60
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": BASE + "/kaitori",
}


def supported_jan(jan):
    return bool(re.fullmatch(r"[0-9]{13}", jan)) and not jan.startswith("20")


def search_quotes(body, jan):
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
    if count > 5:
        return None
    if len(items) != count:
        raise ValueError("Incomplete buyback results")
    quotes = []
    seen = set()
    for item in items:
        # number10=1 is the standard buyback product, excluding retail stock.
        if item.get("keyword2", "").strip() != jan or item.get("number10") != 1:
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
                       "title": item.get("title", ""),
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


class LashinbangQuotes:
    def __init__(self, db_path, lookup=lookup_quote):
        self.db_path = str(db_path)
        self.lookup = lookup
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lashinbang")
        self.guard = threading.Lock()
        self.pending = set()
        self.version = 0
        self.next_request = 0
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS lashinbang_quotes(jan TEXT PRIMARY KEY, data TEXT NOT NULL, retry_at REAL NOT NULL)")

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.db_path, timeout=30)) as db:
            with db:
                yield db

    def cached(self, jan):
        with self.connect() as db:
            row = db.execute("SELECT data,retry_at FROM lashinbang_quotes WHERE jan=?", (jan,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else (None, 0)

    def get_many(self, jans):
        wanted = set(jans)
        with self.connect() as db:
            cached = {row[0]: (json.loads(row[1]), row[2]) for row in db.execute("SELECT jan,data,retry_at FROM lashinbang_quotes") if row[0] in wanted}
        result = {}
        for jan in wanted:
            if not supported_jan(jan):
                result[jan] = {"status": "unsupported", "price_yen": None}
                continue
            quote, retry_at = cached.get(jan, (None, 0))
            self.schedule(jan, cached_retry_at=retry_at)
            with self.guard:
                pending = jan in self.pending
            result[jan] = {**(quote or {"status": "pending", "price_yen": None}), "pending": pending,
                           "stale": bool(quote and (quote.get("stale") or time.time() >= retry_at))}
        return result

    def schedule(self, jan, force=False, cached_retry_at=None):
        if not supported_jan(jan):
            return False
        retry_at = self.cached(jan)[1] if cached_retry_at is None else cached_retry_at
        if not force and time.time() < retry_at:
            return False
        with self.guard:
            if jan in self.pending:
                return False
            self.pending.add(jan)
        try:
            self.executor.submit(self.refresh, jan)
        except Exception:
            with self.guard:
                self.pending.discard(jan)
            raise
        return True

    def refresh(self, jan):
        try:
            time.sleep(max(0, self.next_request - time.monotonic()))
            try:
                quote = self.lookup(jan)
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
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO lashinbang_quotes VALUES(?,?,?)", (jan, json.dumps(quote, ensure_ascii=False), retry_at))
        finally:
            self.next_request = time.monotonic() + 1
            with self.guard:
                self.pending.discard(jan)
                self.version += 1
