"""eBay 公式 Browse API による出品中価格の検索（読み取りのみ）。

キーは環境変数 EBAY_CLIENT_ID / EBAY_CLIENT_SECRET（Production keyset の App ID と Cert ID）。
Browse API で取れるのは「出品中」の価格だけ。売れた価格（落札相場）は API では取得できない
（Marketplace Insights API は審査制）ため、eBay サイトの売り切れ検索へのリンクを返す。
"""
import base64
import statistics
import threading
import time
from urllib.parse import urlencode

import requests

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
CACHE_TTL = 3600
# 為替レート（欧州中央銀行の参照レート。キー不要・平日1回更新）
FX_URL = "https://api.frankfurter.dev/v1/latest"
FX_TTL = 6 * 3600


class EbayNotConfigured(RuntimeError):
    pass


def sold_search_url(term):
    """売れた商品（落札・売り切れ）の検索ページ。API では取れないのでサイトで確認する"""
    return "https://www.ebay.com/sch/i.html?" + urlencode({"_nkw": term, "LH_Sold": "1", "LH_Complete": "1"})


def _money(value):
    try:
        return round(float(value["value"]), 2)
    except (KeyError, TypeError, ValueError):
        return None


def _item(raw):
    price = raw.get("price") or {}
    shipping = None
    options = raw.get("shippingOptions") or []
    if options:
        shipping = _money(options[0].get("shippingCost"))
    return {
        "title": raw.get("title", ""),
        "price": _money(price),
        "currency": price.get("currency", ""),
        "shipping": shipping,
        "condition": raw.get("condition", ""),
        "url": raw.get("itemWebUrl", ""),
        "image": (raw.get("image") or {}).get("imageUrl", ""),
        "country": (raw.get("itemLocation") or {}).get("country", ""),
        "buying_options": raw.get("buyingOptions") or [],
    }


def _summary(items):
    # 通貨が混ざると比べられないので、最も多い通貨（通常USD）だけで集計する
    currencies = [i["currency"] for i in items if i["price"] is not None]
    if not currencies:
        return {"count": 0, "min": None, "median": None, "max": None, "currency": ""}
    currency = max(set(currencies), key=currencies.count)
    prices = sorted(i["price"] for i in items if i["price"] is not None and i["currency"] == currency)
    return {"count": len(prices), "min": prices[0], "median": round(statistics.median(prices), 3),
            "max": prices[-1], "currency": currency}


class FxRates:
    """円換算用の為替レート。取れないときは最後に取れた値、それも無ければ None"""

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.lock = threading.Lock()
        self.rates = {}  # currency -> {"rate", "date", "fetched_at"}

    def to_jpy(self, currency):
        currency = (currency or "").upper()
        if not currency:
            return None
        if currency == "JPY":
            return {"rate": 1.0, "date": ""}
        with self.lock:
            cached = self.rates.get(currency)
            if cached and time.time() - cached["fetched_at"] < FX_TTL:
                return {"rate": cached["rate"], "date": cached["date"]}
            try:
                response = self.session.get(FX_URL, params={"base": currency, "symbols": "JPY"}, timeout=(5, 10))
                response.raise_for_status()
                body = response.json()
                cached = {"rate": float(body["rates"]["JPY"]), "date": body.get("date", ""), "fetched_at": time.time()}
                self.rates[currency] = cached
            except Exception:
                pass  # 為替が取れなくてもeBayの検索結果は返す（ドルのみ表示）
            if not cached:
                return None
            return {"rate": cached["rate"], "date": cached["date"]}


class EbayClient:
    def __init__(self, client_id, client_secret, marketplace="EBAY_US", session=None):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.marketplace = marketplace
        self.session = session or requests.Session()
        self.lock = threading.Lock()
        self.token = None
        self.token_expires = 0
        self.cache = {}

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret)

    def _token(self, force=False):
        with self.lock:
            if self.token and not force and time.time() < self.token_expires:
                return self.token
            basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            response = self.session.post(
                TOKEN_URL,
                headers={"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials", "scope": SCOPE},
                timeout=(5, 15),
            )
            if response.status_code != 200:
                raise RuntimeError(f"eBayの認証に失敗しました（HTTP {response.status_code}）。キーとアカウントの状態を確認してください")
            body = response.json()
            self.token = body["access_token"]
            # 期限の少し前に取り直す（通常2時間有効）
            self.token_expires = time.time() + int(body.get("expires_in", 7200)) - 120
            return self.token

    def _get(self, params):
        for attempt in range(2):
            token = self._token(force=attempt > 0)
            response = self.session.get(
                SEARCH_URL, params=params, timeout=(5, 15),
                headers={"Authorization": "Bearer " + token, "X-EBAY-C-MARKETPLACE-ID": self.marketplace},
            )
            if response.status_code == 401 and attempt == 0:
                continue  # トークン切れ → 1回だけ取り直す
            if response.status_code == 429:
                raise RuntimeError("eBay APIの1日の利用上限に達しました。時間をおいてください")
            if response.status_code != 200:
                raise RuntimeError(f"eBayの検索に失敗しました（HTTP {response.status_code}）")
            return response.json()
        raise RuntimeError("eBayの認証に失敗しました")

    def search(self, jan=None, query=None, japan_only=False, limit=50):
        jan = (jan or "").strip()
        query = (query or "").strip()
        if not jan and not query:
            raise ValueError("JANか検索語を指定してください")
        if not self.configured:
            raise EbayNotConfigured("eBay APIのキーが設定されていません（EBAY_CLIENT_ID / EBAY_CLIENT_SECRET）")
        key = (jan, query, japan_only)
        cached = self.cache.get(key)
        if cached and time.time() - cached["fetched_at"] < CACHE_TTL:
            return cached

        filters = ["buyingOptions:{FIXED_PRICE|AUCTION}"]
        if japan_only:
            filters.append("itemLocationCountry:JP")
        params = {"limit": str(limit), "filter": ",".join(filters)}
        if jan:
            params["gtin"] = jan  # 出品者が登録したJAN/EAN/UPCで完全一致検索
        else:
            params["q"] = query
        body = self._get(params)
        items = [_item(raw) for raw in body.get("itemSummaries") or []]
        result = {
            "mode": "jan" if jan else "keyword",
            "term": jan or query,
            "japan_only": japan_only,
            "total": body.get("total", len(items)),
            "items": items,
            "summary": _summary(items),
            "sold_url": sold_search_url(jan or query),
            "fetched_at": time.time(),
        }
        self.cache[key] = result
        return result
