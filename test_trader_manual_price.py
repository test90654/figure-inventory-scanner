import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from PIL import Image

import trader_integration
from trader_integration import TraderService

JAN = "4900000000016"
MASTER = "123"
EXPECTED = {"model": "M", "name": "Figure"}


def png_bytes(color):
    buffer = io.BytesIO()
    Image.new("RGB", (20, 10), color).save(buffer, format="PNG")
    return buffer.getvalue()


class ManualPriceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.service = TraderService(root / "t.db", root / "data")
        self.image = png_bytes("white")
        name = f"{MASTER}-{'a' * 32}.png"
        (self.service.data_dir / name).write_bytes(self.image)
        product = {"master_id": MASTER, "observed": dict(EXPECTED), "payload": {}, "status": "needs_review",
                   "price_yen": None, "checked_at": "2026-10-19T10:00:00+09:00",
                   "image_url": "/api/trader/image/" + name}
        with self.service.connect() as db:
            db.execute("CREATE TABLE items(jan TEXT PRIMARY KEY, title TEXT)")
            db.execute("INSERT INTO items VALUES(?,?)", (JAN, "Figure"))
            db.execute("INSERT INTO trader_links VALUES(?,?,?,?,?)",
                       (JAN, MASTER, json.dumps(EXPECTED), "Figure", "2026-10-19T09:00:00+09:00"))
            db.execute("INSERT INTO trader_products VALUES(?,?,?)", (MASTER, json.dumps(product), 1000.0))

    def tearDown(self):
        self.service.executor.shutdown(wait=True)
        self.tmp.cleanup()

    def test_unreadable_price_exposes_image_for_review(self):
        quote = self.service.stock_quotes([JAN])[JAN]
        self.assertEqual(quote["status"], "needs_review")
        self.assertEqual(quote["jan"], JAN)
        self.assertTrue(quote["price_image_url"].startswith("/api/trader/image/"))

    def test_manual_price_is_confirmed_and_recorded(self):
        version = self.service.quote_version()
        self.service.confirm_price(JAN, 300)
        quote = self.service.stock_quotes([JAN])[JAN]
        self.assertEqual((quote["status"], quote["price_yen"], quote["price_source"]), ("ok", 300, "manual"))
        self.assertGreater(self.service.quote_version(), version)
        product, updated = self.service.product(MASTER)
        self.assertEqual(updated, 1000.0)  # 確認しただけなので取得時刻(=古さの判定)は変えない
        self.assertEqual(len(product["image_sha256"]), 64)

    def test_invalid_manual_price_or_unlinked_item_is_rejected(self):
        for price in (-1, 10_000_001):
            with self.assertRaises(HTTPException):
                self.service.confirm_price(JAN, price)
        with self.assertRaises(HTTPException):
            self.service.confirm_price("4900000000023", 300)

    def refetch(self, image):
        client = Mock()
        client.fetch.return_value = b"<html></html>"
        client.image.return_value = image
        unread = {"candidate_price_yen": None, "ocr_agreement": False, "ocr_reads": [], "crop_box": None, "digit_count": 3}
        with patch.object(trader_integration, "parse_detail", return_value=(dict(EXPECTED), "src")), \
                patch.object(trader_integration, "read_price", return_value=unread):
            return self.service.get_detail(client, {"master_id": MASTER, "payload": {}}, refresh=True)

    def test_refetch_with_same_price_image_keeps_manual_price(self):
        self.service.confirm_price(JAN, 300)
        record = self.refetch(self.image)
        self.assertEqual((record["status"], record["price_yen"], record["price_source"]), ("ok", 300, "manual"))

    def test_refetch_keeps_manual_price_when_only_the_file_format_differs(self):
        # 実データ: トレーダーから届く画像と保存したPNGはバイト列が異なる（中身の絵は同じ）
        self.service.confirm_price(JAN, 300)
        buffer = io.BytesIO()
        Image.new("RGB", (20, 10), "white").save(buffer, format="BMP")
        record = self.refetch(buffer.getvalue())
        self.assertEqual((record["status"], record["price_yen"]), ("ok", 300))

    def test_refetch_with_changed_price_image_needs_review_again(self):
        self.service.confirm_price(JAN, 300)
        record = self.refetch(png_bytes("black"))
        self.assertEqual(record["status"], "needs_review")
        self.assertIsNone(record["price_yen"])


if __name__ == "__main__":
    unittest.main()
