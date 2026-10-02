import os
import sqlite3
import time
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests

# --- 自動パス取得 ---
base_dir = os.path.dirname(os.path.abspath(__file__))
credential_path = os.path.join(base_dir, "credentials.json")

# --- スプレッドシート接続設定 ---
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
creds = ServiceAccountCredentials.from_json_keyfile_name(
    credential_path, scope
)
client = gspread.authorize(creds)

SPREADSHEET_ID = "1CHnUUP_9uiZYaWzbpoaTIjYwyFY5YBAv4x5M3gka2T0"
sheet = client.open_by_key(SPREADSHEET_ID).sheet1


def bulk_import_all_pasomaru():
    print("🚀 ぱそまる全商品の一括インポート（全ページ自動走査）を開始します...")

    # 1. ヘッダー自動作成
    if not sheet.row_values(1):
        print("📝 ヘッダー行を自動作成します...")
        sheet.append_row(["JAN", "商品名", "画像URL", "買取価格"])

    # 2. 既存のJANコードを読み込み
    print("📖 スプレッドシートから既存のデータを読み込み中...")
    existing_records = sheet.get_all_values()
    existing_jans = set()
    for row in existing_records:
        if len(row) > 0 and row[0]:
            existing_jans.add(row[0])
    print(f"📌 既存の登録済みJAN数: {len(existing_jans)} 件")

    page = 1
    total_new_added = 0

    while True:
        url = f"https://www.pasomaru.co.jp/products?page={page}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }

        print(f"📖 ページ {page} を取得中... ({url})")
        
        # 接続エラーやタイムアウト対策（リトライ＆長めのタイムアウト）
        response = None
        for retry in range(3):
            try:
                response = requests.get(url, headers=headers, timeout=30)
                break
            except requests.exceptions.RequestException as e:
                print(f"⚠️ 接続エラー発生 (試行 {retry + 1}/3): {e}")
                time.sleep(3)

        if response is None or response.status_code != 200:
            status = response.status_code if response else "不明"
            print(f"⚠️ ページ取得に失敗したため、全ページの走査を終了します (ステータス: {status})")
            break

        soup = BeautifulSoup(response.text, "html.parser")
        product_rows = soup.find_all("div", class_="products-row")

        # 該当ページに商品が1つもなければ終了
        if not product_rows:
            print(f"🏁 ページ {page} に商品がないため、全ページの走査を終了します。")
            break

        new_rows_in_page = []
        for row in product_rows:
            # 商品名
            header_div = row.find("div", class_="products-row-header")
            raw_title = header_div.text if header_div else "不明"

            # 改行や大量の空白を1行にきれいにまとめる
            clean_title = " ".join(raw_title.split())

            # 「特別指定」が含まれている場合は不要なため削除する
            if "特別指定" in clean_title:
                clean_title = clean_title.replace("特別指定", "").strip()

            # 画像URL
            img_tag = row.find("img", class_="products-image")
            image_url = img_tag["src"] if img_tag else ""

            # 買取価格
            price_div = row.find("div", class_="products-price-value")
            price = price_div.text.strip() if price_div else "価格不明"

            # JAN/型番
            jan = ""
            info_lines = row.find_all("div", class_="products-info-line")
            for line in info_lines:
                label = line.find("span", class_="products-info-label")
                if label and "JAN" in label.text:
                    jan = line.find_all("span")[-1].text.strip()
                    break

            # 未登録のJANのみ追加リストへ
            if jan and jan not in existing_jans:
                new_rows_in_page.append([jan, clean_title, image_url, price])
                existing_jans.add(jan)

        # このページで見つかった新着データをまとめてスプレッドシートに追記
        if new_rows_in_page:
            sheet.append_rows(new_rows_in_page)
            total_new_added += len(new_rows_in_page)
            print(
                f"✅ ページ {page}: 新規 {len(new_rows_in_page)} 件を追加（累計追加:"
                f" {total_new_added}件）"
            )
        else:
            print(
                f"ℹ️️ ページ {page}: すべて既存登録済みの商品でした（新規追加 0 件）"
            )

        # サーバーへのマナーとして1秒待つ
        time.sleep(1)
        page += 1

    print(
        f"\n🎉 すべての処理が完了しました！ 今回新たに追加された総件数: {total_new_added}"
        " 件"
    )


if __name__ == "__main__":
    bulk_import_all_pasomaru()
