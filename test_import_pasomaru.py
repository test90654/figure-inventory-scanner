import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from import_pasomaru import bulk_import_all_pasomaru


def product(jan="known", title="Name"):
    return (f'<div class="products-row"><div class="products-row-header">{title}</div>'
            '<div class="products-info-line"><span class="products-info-label">JAN</span>'
            f'<span>{jan}</span></div></div>')


class ImportTests(unittest.TestCase):
    def run_import(self, pages, records=None, **kwargs):
        sheet = Mock()
        sheet.get_all_values.return_value = records if records is not None else [
            ["JAN", "商品名"], ["known", "Name"]]
        responses = [Mock(status_code=200, text=html) for html in pages]
        with patch("import_pasomaru.requests.get", side_effect=responses) as get, \
                patch("import_pasomaru.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            bulk_import_all_pasomaru(sheet, **kwargs)
        return sheet, get.call_count

    def test_default_stops_after_five_known_pages(self):
        sheet, count = self.run_import([product()] * 5)
        self.assertEqual(count, 5)
        sheet.append_rows.assert_not_called()

    def test_new_product_resets_consecutive_count_and_is_not_duplicated(self):
        sheet, count = self.run_import(
            [product(), product("new"), product("new"), product()], max_known_pages=2)
        self.assertEqual(count, 4)
        sheet.append_rows.assert_called_once_with([["new", "Name", "", "価格不明"]])

    def test_full_scan_reaches_new_product_beyond_known_pages(self):
        sheet, count = self.run_import(
            [product(), product(), product("deep"), ""], full_scan=True, max_known_pages=1)
        self.assertEqual(count, 4)
        self.assertEqual(sheet.append_rows.call_args.args[0][0][0], "deep")

    def test_first_import_ignores_early_stop(self):
        sheet, count = self.run_import(
            [product("new"), product("new"), product("deep"), ""],
            records=[["JAN", "商品名"]], max_known_pages=1)
        self.assertEqual(count, 4)
        self.assertEqual(sheet.append_rows.call_count, 2)

    def test_name_repair_runs_before_early_stop(self):
        sheet, count = self.run_import([product(title="Name extended")], max_known_pages=1)
        sheet.batch_update.assert_called_once_with(
            [{"range": "B2", "values": [["Name extended"]]}])
        self.assertEqual(count, 1)

    def test_missing_jan_breaks_known_streak(self):
        _, count = self.run_import(
            [product(), product(""), product(), product()], max_known_pages=2)
        self.assertEqual(count, 4)

    def test_empty_page_ends_scan(self):
        _, count = self.run_import([""])
        self.assertEqual(count, 1)

    def test_http_error_fails_run(self):
        sheet = Mock()
        sheet.get_all_values.return_value = [["JAN"], ["known"]]
        with patch("import_pasomaru.requests.get", return_value=Mock(status_code=503)), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            bulk_import_all_pasomaru(sheet)

    def test_timeout_fails_after_retries(self):
        import requests
        sheet = Mock()
        sheet.get_all_values.return_value = [["JAN"], ["known"]]
        with patch("import_pasomaru.requests.get", side_effect=requests.exceptions.Timeout) as get, \
                patch("import_pasomaru.time.sleep"), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(RuntimeError):
            bulk_import_all_pasomaru(sheet)
        self.assertEqual(get.call_count, 3)

    def test_invalid_threshold_rejected_before_connect(self):
        with patch("import_pasomaru.connect_sheet") as connect, self.assertRaises(ValueError):
            bulk_import_all_pasomaru(max_known_pages=0)
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
