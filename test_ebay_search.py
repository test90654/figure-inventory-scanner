import base64
import unittest
from unittest.mock import Mock

import ebay_search
from ebay_search import EbayClient, EbayNotConfigured

JAN = "4983164873351"


def token_response(token="T1", expires_in=7200):
    return Mock(status_code=200, json=Mock(return_value={"access_token": token, "expires_in": expires_in}))


def search_response(items, total=None):
    body = {"total": len(items) if total is None else total, "itemSummaries": items}
    return Mock(status_code=200, json=Mock(return_value=body))


ITEM = {
    "title": "Banpresto Vibration Stars Sabito Figure",
    "price": {"value": "24.99", "currency": "USD"},
    "shippingOptions": [{"shippingCost": {"value": "15.00", "currency": "USD"}}],
    "condition": "New",
    "itemWebUrl": "https://www.ebay.com/itm/1",
    "image": {"imageUrl": "https://i.ebayimg.com/1.jpg"},
    "itemLocation": {"country": "JP"},
    "buyingOptions": ["FIXED_PRICE"],
}


class EbayClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.client = EbayClient("CID", "SECRET", session=self.session)

    def test_not_configured_without_keys(self):
        with self.assertRaises(EbayNotConfigured):
            EbayClient("", "").search(jan=JAN)

    def test_application_token_uses_client_credentials(self):
        self.session.post.return_value = token_response()
        self.session.get.return_value = search_response([ITEM])
        self.client.search(jan=JAN)
        url = self.session.post.call_args.args[0]
        kwargs = self.session.post.call_args.kwargs
        self.assertEqual(url, "https://api.ebay.com/identity/v1/oauth2/token")
        self.assertEqual(kwargs["headers"]["Authorization"], "Basic " + base64.b64encode(b"CID:SECRET").decode())
        self.assertEqual(kwargs["data"], {"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"})

    def test_token_is_reused_until_expiry(self):
        self.session.post.return_value = token_response()
        self.session.get.return_value = search_response([ITEM])
        self.client.search(jan=JAN)
        self.client.search(query="sabito figure")
        self.assertEqual(self.session.post.call_count, 1)

    def test_jan_search_uses_gtin_and_parses_items(self):
        self.session.post.return_value = token_response()
        self.session.get.return_value = search_response([ITEM, {**ITEM, "price": {"value": "40.00", "currency": "USD"}, "shippingOptions": []}])
        result = self.client.search(jan=JAN)
        params = self.session.get.call_args.kwargs["params"]
        headers = self.session.get.call_args.kwargs["headers"]
        self.assertEqual(params["gtin"], JAN)
        self.assertNotIn("q", params)
        self.assertEqual(headers["X-EBAY-C-MARKETPLACE-ID"], "EBAY_US")
        self.assertEqual(headers["Authorization"], "Bearer T1")
        first = result["items"][0]
        self.assertEqual((first["price"], first["currency"], first["shipping"]), (24.99, "USD", 15.0))
        self.assertEqual((first["country"], first["url"]), ("JP", "https://www.ebay.com/itm/1"))
        self.assertIsNone(result["items"][1]["shipping"])
        self.assertEqual(result["summary"], {"count": 2, "min": 24.99, "median": 32.495, "max": 40.0, "currency": "USD"})

    def test_keyword_search_and_japan_filter(self):
        self.session.post.return_value = token_response()
        self.session.get.return_value = search_response([])
        result = self.client.search(query="sabito vibration stars", japan_only=True)
        params = self.session.get.call_args.kwargs["params"]
        self.assertEqual(params["q"], "sabito vibration stars")
        self.assertIn("itemLocationCountry:JP", params["filter"])
        self.assertEqual(result["summary"]["count"], 0)
        self.assertIn("LH_Sold=1", result["sold_url"])  # 売れた価格はeBayのサイトで確認する

    def test_expired_token_is_refreshed_once(self):
        self.session.post.side_effect = [token_response("OLD"), token_response("NEW")]
        self.session.get.side_effect = [Mock(status_code=401, json=Mock(return_value={})), search_response([ITEM])]
        result = self.client.search(jan=JAN)
        self.assertEqual(result["summary"]["count"], 1)
        self.assertEqual(self.session.get.call_args.kwargs["headers"]["Authorization"], "Bearer NEW")

    def test_results_are_cached(self):
        self.session.post.return_value = token_response()
        self.session.get.return_value = search_response([ITEM])
        self.client.search(jan=JAN)
        self.client.search(jan=JAN)
        self.assertEqual(self.session.get.call_count, 1)

    def test_requires_jan_or_query(self):
        with self.assertRaises(ValueError):
            self.client.search()


if __name__ == "__main__":
    unittest.main()
