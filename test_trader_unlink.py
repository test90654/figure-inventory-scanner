import json
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from PIL import Image

from trader_integration import TraderService
from trader_sheets import SheetEventStore

JAN = "4900000000016"
MASTER = "123"
EXPECTED = {"model": "M", "name": "Figure"}


class FakeSheet:
    """Google Sheets の代わりに、実際の保存形式(pack/unpack)を通して記録を貯める"""

    def __init__(self, data_dir):
        self.codec = SheetEventStore(lambda: None, data_dir)
        self.rows = []

    def append_event(self, event):
        self.rows.append(self.codec.pack(event))

    def load_events(self):
        return [self.codec.unpack(row) for row in self.rows]


def make_service(root, name, sheet=None):
    service = TraderService(root / f"{name}.db", root / f"{name}-data")
    service.sheet_store = sheet
    with service.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS items(jan TEXT PRIMARY KEY, title TEXT)")
        db.execute("INSERT OR REPLACE INTO items VALUES(?,?)", (JAN, "Figure"))
    return service


class UnlinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        # 本番と同じく、記録の画像はアプリの画像フォルダを使う
        self.sheet = FakeSheet(self.root / "a-data")
        self.service = make_service(self.root, "a", self.sheet)
        name = f"{MASTER}-{'a' * 32}.png"
        Image.new("RGB", (20, 10), "white").save(self.service.data_dir / name)
        product = {"master_id": MASTER, "observed": dict(EXPECTED), "payload": {}, "status": "ok", "price_yen": 500,
                   "checked_at": "2026-10-19T10:00:00+09:00", "image_url": "/api/trader/image/" + name}
        with self.service.connect() as db:
            db.execute("INSERT INTO trader_products VALUES(?,?,?)", (MASTER, json.dumps(product), 1000.0))
        self.service.choose(JAN, MASTER)  # 間違えて紐づけた

    def tearDown(self):
        self.service.executor.shutdown(wait=True)
        self.tmp.cleanup()

    def test_unlink_returns_item_to_unlinked(self):
        self.assertEqual(self.service.stock_quotes([JAN])[JAN]["price_yen"], 500)
        version = self.service.quote_version()
        state = self.service.unlink(JAN)
        self.assertIsNone(state["link"])
        self.assertEqual(self.service.stock_quotes([JAN])[JAN]["status"], "unlinked")
        self.assertGreater(self.service.quote_version(), version)

    def test_unlink_without_link_is_rejected(self):
        self.service.unlink(JAN)
        with self.assertRaises(HTTPException):
            self.service.unlink(JAN)

    def test_unlink_is_saved_and_survives_restore(self):
        self.service.unlink(JAN)
        kinds = [self.sheet.codec.unpack(row)["kind"] for row in self.sheet.rows]
        self.assertEqual(kinds, ["select", "unlink"])
        # 再起動（ローカルDBが消えた）後に、記録から復元しても解除されたまま
        restored = make_service(self.root, "b", self.sheet)
        try:
            self.assertEqual(restored.stock_quotes([JAN])[JAN]["status"], "unlinked")
        finally:
            restored.executor.shutdown(wait=True)

    def test_relink_after_unlink_is_restored(self):
        self.service.unlink(JAN)
        self.service.choose(JAN, MASTER)
        restored = make_service(self.root, "c", self.sheet)
        try:
            restored.ensure_restored()  # 記録からの復元は最初の利用時に行われる
            with restored.connect() as db:
                row = db.execute("SELECT master_id FROM trader_links WHERE jan=?", (JAN,)).fetchone()
            self.assertEqual(row["master_id"], MASTER)
        finally:
            restored.executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
