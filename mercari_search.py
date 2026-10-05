#! /usr/bin/env python3
"""メルカリの検索結果（任意のキーワード）を取得する

アプリからは search_mercari() / price_summary() を利用する。
単体でも CSV 出力ツールとして使える:
    python mercari_search.py "初音ミク Luminasta"
    python mercari_search.py "ちいかわ ぬいぐるみ" --pages 5 --status sold_out
    python mercari_search.py "ワンピース DXF" --min 1000 --max 5000 --sort created_time
"""

import argparse
import csv
import re
import statistics
import time
from datetime import datetime
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

BASE_URL = "https://jp.mercari.com/search"
NO_RESULT_TEXT = "出品された商品がありません"

# 並び順: score=おすすめ順
SORTS = {
    "score": {},
    "created_time": {"sort": "created_time", "order": "desc"},
    "price_asc": {"sort": "price", "order": "asc"},
    "price_desc": {"sort": "price", "order": "desc"},
}

# 画面内に描画されている商品セルを抜き出す
EXTRACT_JS = """
() => [...document.querySelectorAll('[data-testid="item-cell"]')].map(cell => {
  const a = cell.querySelector('a[data-testid="thumbnail-link"]');
  const img = cell.querySelector('img');
  const price = cell.querySelector('[data-testid="item-tile-price"]');
  return {
    link: a ? a.href : '',
    alt: img ? img.alt : '',
    price: price ? price.innerText : '',
    sold: !!cell.querySelector('[data-testid="item-tile-sticker"]'),
  };
})
"""


def build_url(keyword, page=0, status=None, price_min=None, price_max=None, sort="score"):
  params = {"keyword": keyword}
  params.update(SORTS[sort])
  if status:
    params["status"] = status  # on_sale / sold_out
  if price_min:
    params["price_min"] = price_min
  if price_max:
    params["price_max"] = price_max
  if page > 0:
    params["page_token"] = f"v1:{page}"
  return f"{BASE_URL}?{urlencode(params)}"


def _normalize(raw):
  m = re.search(r"/item/(m\d+)", raw["link"])
  return {
      "title": re.sub(r"のサムネイル.*$", "", raw["alt"]),
      "sold": raw["sold"],
      "price": int(re.sub(r"[^\d]", "", raw["price"]) or 0),
      "thumb": f"https://static.mercdn.net/thumb/item/webp/{m.group(1)}_1.jpg" if m else "",
      "link": raw["link"],
  }


def search_mercari(
    keyword,
    pages=1,
    status=None,
    price_min=None,
    price_max=None,
    sort="score",
    interval=5.0,
    headless=True,
    log=print,
):
  """検索結果を dict のリストで返す（1ページ約120件, 最大100ページ）"""
  results, seen = [], set()
  with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=headless, args=["--no-sandbox", "--disable-setuid-sandbox"]
    )
    page = browser.new_page(viewport={"width": 1280, "height": 2000})
    try:
      for n in range(min(pages, 100)):
        url = build_url(keyword, n, status, price_min, price_max, sort)
        log(f"[{n + 1}/{pages}] {url}")
        page.goto(url, timeout=30000)
        try:
          # 「該当なし」の文言は page source には常に含まれるため表示テキストで判定
          page.wait_for_function(
              f"""() => document.querySelector('[data-testid="item-cell"]')
                  || document.body.innerText.includes('{NO_RESULT_TEXT}')""",
              timeout=30000,
          )
        except Exception:
          log("  読み込みタイムアウト → 終了")
          break
        if not page.query_selector('[data-testid="item-cell"]'):
          log("  商品なし → 終了")
          break

        # 商品は画面に入った分だけ描画されるので、少しずつスクロールしながら集める
        added, last_y = 0, -1
        while True:
          for raw in page.evaluate(EXTRACT_JS):
            if raw["link"] and raw["link"] not in seen:
              seen.add(raw["link"])
              results.append(_normalize(raw))
              added += 1
          page.evaluate("window.scrollBy(0, 800)")
          page.wait_for_timeout(700)
          y = page.evaluate("window.scrollY")
          if y == last_y:
            break
          last_y = y
        log(f"  {added} 件取得（累計 {len(results)} 件）")

        if not page.query_selector('[data-testid="pagination-next-button"]'):
          break
        if n + 1 < pages:
          time.sleep(interval)
    finally:
      browser.close()
  return results


def price_summary(items):
  """価格の統計（0円=価格不明は除外）"""
  prices = sorted(i["price"] for i in items if i["price"] > 0)
  if not prices:
    return {"count": 0, "min": None, "median": None, "max": None, "average": None}
  return {
      "count": len(prices),
      "min": prices[0],
      "median": int(statistics.median(prices)),
      "max": prices[-1],
      "average": int(statistics.mean(prices)),
  }


def main():
  ap = argparse.ArgumentParser(description="メルカリ検索結果を CSV に保存")
  ap.add_argument("keyword", help="検索キーワード")
  ap.add_argument("--pages", type=int, default=3, help="取得ページ数（1ページ約120件, 最大100）")
  ap.add_argument("--status", choices=["on_sale", "sold_out"], help="販売中 / 売り切れのみ")
  ap.add_argument("--min", type=int, dest="price_min", help="最低価格")
  ap.add_argument("--max", type=int, dest="price_max", help="最高価格")
  ap.add_argument("--sort", choices=SORTS.keys(), default="score", help="並び順")
  ap.add_argument("--interval", type=float, default=5.0, help="ページ間の待機秒数")
  ap.add_argument("--show", action="store_true", help="ブラウザを表示する")
  ap.add_argument("-o", "--output", help="出力CSVファイル名")
  args = ap.parse_args()

  results = search_mercari(
      args.keyword, args.pages, args.status, args.price_min, args.price_max,
      args.sort, args.interval, headless=not args.show,
  )

  safe_kw = re.sub(r'[\\/:*?"<>| ]+', "_", args.keyword)
  out = args.output or f"mercari_{safe_kw}_{datetime.now():%Y%m%d_%H%M%S}.csv"
  with open(out, "w", newline="", encoding="utf-8-sig") as f:  # Excel 用に BOM 付き
    w = csv.DictWriter(f, fieldnames=["title", "sold", "price", "thumb", "link"])
    w.writeheader()
    w.writerows(results)
  print(f"保存しました: {out}（{len(results)} 件）")
  print(price_summary(results))


if __name__ == "__main__":
  main()
