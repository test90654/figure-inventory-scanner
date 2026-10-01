import datetime
import io
import json
import os
import sqlite3
import traceback
import urllib.parse
from bs4 import BeautifulSoup
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel
import requests
from playwright.sync_api import sync_playwright

app = FastAPI(title="Figure Inventory Cloud Edition")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

base_dir = os.path.dirname(os.path.abspath(__file__))
credential_path = os.path.join(base_dir, "credentials.json")
db_path = os.path.join(base_dir, "inventory.db")
img_cache_dir = os.path.join(base_dir, "img_cache")
os.makedirs(img_cache_dir, exist_ok=True)

if not os.path.exists(credential_path):
  gcp_json_env = os.environ.get("GCP_CREDENTIALS_JSON")
  if gcp_json_env:
    with open(credential_path, "w", encoding="utf-8") as f:
      f.write(gcp_json_env)

# ==========================================
# 1. 引用元の自動判定ロジック[cite: 1]
# ==========================================


def resolve_quote_meta(title: str):
  t = title.lower()
  if any(
      k in t
      for k in [
          "一番くじ",
          "バンプレスト",
          "banpresto",
          "grandista",
          "q posket",
          "relax time",
          "vibration stars",
          "solid and souls",
          "dxf",
          "fluffy puffy",
          "masterlise",
          "wcf",
      ]
  ):
    return {
        "maker": "BANDAI SPIRITS",
        "quote_label": "バンプレスト / バンプレナビ",
        "source_url": "https://bsp-prize.jp/",
    }
  elif any(
      k in t
      for k in [
          "luminasta",
          "ちょこのせ",
          "tip'n'pop",
          "セガ",
          "sega",
          "spm",
          "プレミアムフィギュア",
      ]
  ):
    return {
        "maker": "SEGA",
        "quote_label": "セガ / セガプラザ",
        "source_url": "https://segaplaza.jp/",
    }
  elif any(
      k in t
      for k in [
          "amp+",
          "coreful",
          "タイトー",
          "taito",
          "aqua float",
          "desktop cute",
          "プチエット",
      ]
  ):
    return {
        "maker": "タイトー",
        "quote_label": "タイトー / タイトープライズ",
        "source_url": "https://www.taito.co.jp/taito-prize",
    }
  elif any(
      k in t
      for k in [
          "bicute",
          "trio-try-it",
          "フリュー",
          "furyu",
          "tenitol",
          "ぬーどるストッパー",
          "ひっかけ",
      ]
  ):
    return {
        "maker": "フリュー",
        "quote_label": "フリュー / キャラ広場",
        "source_url": "https://charahiroba.com/prize/",
    }
  else:
    encoded = urllib.parse.quote(title)
    return {
        "maker": "公式・流通",
        "quote_label": "流通 / 商品カタログ",
        "source_url": f"https://www.google.com/search?q={encoded}",
    }


# ==========================================
# 2. DB初期化[cite: 1]
# ==========================================


def init_db():
  conn = sqlite3.connect(db_path)
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS items (
            jan TEXT PRIMARY KEY,
            title TEXT,
            image_url TEXT,
            price TEXT,
            maker TEXT DEFAULT '公式・流通',
            quote_label TEXT DEFAULT '流通 / 商品カタログ',
            source_url TEXT DEFAULT ''
        )
    """)
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS unregistered_logs (
            jan TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
  for col, col_def in [
      ("maker", "TEXT DEFAULT '公式・流通'"),
      ("quote_label", "TEXT DEFAULT '流通 / 商品カタログ'"),
      ("source_url", "TEXT DEFAULT ''"),
  ]:
    try:
      cursor.execute(f"ALTER TABLE items ADD COLUMN {col} {col_def}")
    except sqlite3.OperationalError:
      pass

  conn.commit()
  conn.close()


init_db()


def get_gspread_sheets():
  scope = [
      "https://spreadsheets.google.com/feeds",
      "https://www.googleapis.com/auth/drive",
  ]
  creds = ServiceAccountCredentials.from_json_keyfile_name(
      credential_path, scope
  )
  client = gspread.authorize(creds)
  SPREADSHEET_ID = "1CHnUUP_9uiZYaWzbpoaTIjYwyFY5YBAv4x5M3gka2T0"
  spreadsheet = client.open_by_key(SPREADSHEET_ID)

  main_sheet = spreadsheet.sheet1
  try:
    unreg_sheet = spreadsheet.worksheet("未登録リスト")
  except gspread.exceptions.WorksheetNotFound:
    unreg_sheet = spreadsheet.add_worksheet(
        title="未登録リスト", rows="1000", cols="2"
    )
    unreg_sheet.append_row(["JAN", "発見日時"])

  return main_sheet, unreg_sheet


def search_neatz_fallback(query_code: str):
  try:
    search_url = "https://www.neatzanime.cz/search-engine.htm"
    params = {"slovo": query_code, "hledatjak": "2"}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ),
        "Referer": "https://www.neatzanime.cz/",
    }
    response = requests.get(
        search_url, params=params, headers=headers, timeout=3.0
    )
    if response.status_code != 200:
      return None

    soup = BeautifulSoup(response.text, "html.parser")
    product_div = soup.select_one("div.product")
    if not product_div:
      return None

    img_tag = product_div.select_one(".img_box img")
    image_url = img_tag.get("src", "") if img_tag else ""
    return image_url if image_url else None
  except Exception:
    return None


def get_image_bytes(jan: str, target_url: str):
  local_file = os.path.join(img_cache_dir, f"{jan}.jpg")
  if os.path.exists(local_file) and os.path.getsize(local_file) > 500:
    with open(local_file, "rb") as f:
      return f.read()

  if target_url and target_url.startswith("local_upload:"):
    if os.path.exists(local_file):
      with open(local_file, "rb") as f:
        return f.read()
    return None

  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
      ),
      "Referer": "https://www.neatzanime.cz/",
  }
  if target_url and target_url.startswith("/"):
    target_url = "https://www.pasomaru.co.jp" + target_url

  res = None
  if target_url and not target_url.startswith("local_upload:"):
    try:
      res = requests.get(target_url, headers=headers, timeout=3.0)
    except Exception:
      res = None

  if (not res or res.status_code != 200) and jan:
    fixed_url = search_neatz_fallback(jan)
    if fixed_url:
      try:
        res = requests.get(fixed_url, headers=headers, timeout=3.0)
        if res and res.status_code == 200:
          conn = sqlite3.connect(db_path)
          c = conn.cursor()
          c.execute(
              "UPDATE items SET image_url = ? WHERE jan = ?", (fixed_url, jan)
          )
          conn.commit()
          conn.close()
      except Exception:
        res = None

  if res and res.status_code == 200:
    try:
      image = Image.open(io.BytesIO(res.content)).convert("RGB")
      buf = io.BytesIO()
      image.save(buf, format="JPEG", quality=90)
      jpeg_bytes = buf.getvalue()

      if os.path.exists(local_file):
        os.remove(local_file)

      with open(local_file, "wb") as f:
        f.write(jpeg_bytes)
      return jpeg_bytes
    except Exception as img_err:
      print(f"⚠️️ 画像変換エラー (JAN: {jan}): {img_err}")
      with open(local_file, "wb") as f:
        f.write(res.content)
      return res.content

  return None


def sync_sheet_to_local():
  if not os.path.exists(credential_path):
    print("⚠️ credentials.json が見つかりません。")
    return
  try:
    main_sheet, _ = get_gspread_sheets()
    rows = main_sheet.get_all_values()

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    count = 0
    for row in rows:
      if len(row) >= 4 and row[0].strip():
        jan = row[0].strip()
        if jan.upper() == "JAN" or not jan.isdigit():
          continue
        title = row[1].strip()
        image_url = row[2].strip()
        price = row[3].strip()

        meta = resolve_quote_meta(title)
        cursor.execute(
            """
                INSERT OR REPLACE INTO items (jan, title, image_url, price, maker, quote_label, source_url)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                jan,
                title,
                image_url,
                price,
                meta["maker"],
                meta["quote_label"],
                meta["source_url"],
            ),
        )
        count += 1

    conn.commit()
    conn.close()
    print(f"🚀 スプレッドシートから {count} 件同期完了！")
  except Exception as e:
    print(f"⚠️ スプレッドシート同期スキップ: {e}")


sync_sheet_to_local()


@app.get("/proxy_image")
def proxy_image(url: str = None, jan: str = None, refresh: int = 0):
  if refresh == 1 and jan:
    local_file = os.path.join(img_cache_dir, f"{jan}.jpg")
    if os.path.exists(local_file) and not (
        url and url.startswith("local_upload:")
    ):
      try:
        os.remove(local_file)
      except Exception:
        pass

  target_url = urllib.parse.unquote(url) if url else ""
  data = get_image_bytes(jan or "temp", target_url)
  if data:
    return Response(content=data, media_type="image/jpeg")
  raise HTTPException(status_code=404, detail="Image not found")


class ScanRequest(BaseModel):
  jan: str


class CartItem(BaseModel):
  jan: str
  title: str = ""
  count: int = 1
  image_url: str = ""
  quote_label: str = ""
  source_url: str = ""


class SheetRenderRequest(BaseModel):
  items: list[CartItem]
  page: int
  total_pages: int


class BulkAddRequest(BaseModel):
  jans: list[str]


@app.post("/api/bulk_add_box")
def bulk_add_box(data: BulkAddRequest):
  results = []
  try:
    with sync_playwright() as p:
      browser = p.chromium.launch(
          headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"]
      )
      context = browser.new_context(
          user_agent=(
              "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
              " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
          )
      )
      page = context.new_page()

      page.goto("https://shop.lashinbang.com/kaitori/offer", timeout=15000)
      page.wait_for_timeout(2000)

      for jan in data.jans:
        jan = jan.strip()
        if not jan:
          continue

        try:
          search_url = f"https://shop.lashinbang.com/kaitori/list?jan={jan}"
          page.goto(search_url, timeout=10000)
          page.wait_for_selector("li[data-product-id]", timeout=5000)

          product_id = page.eval_on_selector(
              "li[data-product-id]", "el => el.getAttribute('data-product-id')"
          )

          if not product_id:
            results.append({"jan": jan, "status": "not_found"})
            continue

          add_btn = page.query_selector("a.item_add_cart_btn")
          if add_btn:
            add_btn.click()
            page.wait_for_timeout(1500)

            close_popup_btn = page.query_selector(
                "#addCartOK button.modalClose, .modal_addCart_offer_button"
                " button.modalClose"
            )
            if close_popup_btn:
              close_popup_btn.click()
              page.wait_for_timeout(1000)

            results.append({"jan": jan, "status": "success", "id": product_id})
          else:
            results.append(
                {"jan": jan, "status": "button_not_found", "id": product_id}
            )

        except Exception as e:
          results.append({"jan": jan, "status": "error", "message": str(e)})

        page.wait_for_timeout(1000)

      browser.close()
  except Exception as e:
    traceback.print_exc()
    raise HTTPException(status_code=500, detail=str(e))

  return {"status": "completed", "results": results}


@app.post("/manual_register")
async def manual_register(
    jan: str = Form(...),
    title: str = Form(...),
    price: str = Form("買取中!!"),
    image_url: str = Form(""),
    file: UploadFile = File(None),
):
  try:
    cleanText = "".join(filter(str.isdigit, jan))

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT title, price FROM items WHERE jan = ?", (cleanText,))
    existing = cursor.fetchone()

    final_title = (
        title
        if (title and title.strip())
        else (
            existing[0]
            if existing and existing[0]
            else f"プライズフィギュア ({cleanText})"
        )
    )
    final_price = existing[1] if (existing and existing[1]) else price

    final_image_url = image_url

    if file and file.filename:
      contents = await file.read()
      if contents:
        local_file = os.path.join(img_cache_dir, f"{cleanText}.jpg")
        image = Image.open(io.BytesIO(contents)).convert("RGB")
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=90)
        with open(local_file, "wb") as f:
          f.write(buf.getvalue())
        final_image_url = "local_upload:" + file.filename

    meta = resolve_quote_meta(final_title)

    cursor.execute(
        """
            INSERT OR REPLACE INTO items (jan, title, image_url, price, maker, quote_label, source_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cleanText,
            final_title,
            final_image_url,
            final_price,
            meta["maker"],
            meta["quote_label"],
            meta["source_url"],
        ),
    )
    conn.commit()
    conn.close()

    try:
      main_sheet, _ = get_gspread_sheets()
      rows = main_sheet.get_all_values()
      found_row = -1
      for idx, row in enumerate(rows[1:], start=2):
        if len(row) > 0 and row[0].strip() == cleanText:
          found_row = idx
          break

      if found_row != -1:
        main_sheet.update_cell(found_row, 2, final_title)
        if final_image_url:
          main_sheet.update_cell(found_row, 3, final_image_url)
      else:
        main_sheet.append_row(
            [cleanText, final_title, final_image_url, final_price]
        )
    except Exception as e:
      print(f"⚠️ スプレッドシートの手動登録/画像更新スキップ: {e}")

    return {
        "status": "success",
        "jan": cleanText,
        "title": final_title,
        "price": final_price,
        "image_url": final_image_url,
        "quote_label": meta["quote_label"],
        "source_url": meta["source_url"],
    }
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))


@app.post("/generate_sheet")
def generate_sheet(req: SheetRenderRequest):
  canvas_w, canvas_h = 900, 1100
  img_out = Image.new("RGB", (canvas_w, canvas_h), color=(30, 30, 30))
  draw = ImageDraw.Draw(img_out)

  font_large, font_badge, font_small = None, None, None
  font_paths = [
      "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
      "/usr/share/fonts/truetype/ipafont-gothic/ipag.ttf",
      "C:\\Windows\\Fonts\\msgothic.ttc",
      "msgothic.ttc",
  ]
  for fp in font_paths:
    if os.path.exists(fp):
      try:
        font_large = ImageFont.truetype(fp, 26)
        font_badge = ImageFont.truetype(fp, 52)
        font_small = ImageFont.truetype(fp, 12)
        break
      except Exception:
        continue

  if not font_large:
    font_large = ImageFont.load_default()
    font_badge = ImageFont.load_default()
    font_small = ImageFont.load_default()

  header_text = f"買取パッケージ一覧 (シート {req.page} / {req.total_pages})"
  draw.text((40, 40), header_text, fill=(0, 255, 204), font=font_large)

  cols = 3
  startX, startY = 40, 95
  cellW, cellH = 260, 305
  gapX, gapY = 30, 25

  for index, item in enumerate(req.items):
    col = index % cols
    row = index // cols
    x = startX + col * (cellW + gapX)
    y = startY + row * (cellH + gapY)

    draw.rectangle([x, y, x + cellW, y + cellH], fill=(44, 44, 44))

    raw_bytes = get_image_bytes(item.jan, item.image_url)
    if raw_bytes:
      try:
        tile = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        tile_w, tile_h = tile.size
        img_area_h = cellH - 35
        scale = max(cellW / tile_w, img_area_h / tile_h)
        nw, nh = int(tile_w * scale), int(tile_h * scale)
        tile = tile.resize((nw, nh), Image.Resampling.BILINEAR)

        crop_x = (nw - cellW) // 2
        crop_y = (nh - img_area_h) // 2
        tile = tile.crop((crop_x, crop_y, crop_x + cellW, crop_y + img_area_h))
        img_out.paste(tile, (x, y))
      except Exception:
        pass
    else:
      draw.text(
          (x + 80, y + 100), "No Image", fill=(255, 204, 0), font=font_large
      )

    draw.rectangle([x, y, x + cellW, y + cellH], outline=(80, 80, 80), width=2)

    badge = f"{item.count}個"
    draw.rectangle(
        [
            x + cellW // 2 - 55,
            y + (cellH - 35) // 2 - 25,
            x + cellW // 2 + 55,
            y + (cellH - 35) // 2 + 25,
        ],
        fill=(0, 0, 0, 180),
    )
    draw.text(
        (x + cellW // 2 - 40, y + (cellH - 35) // 2 - 22),
        badge,
        fill=(0, 255, 102),
        font=font_badge,
    )

    label = item.quote_label if item.quote_label else "流通 / 商品カタログ"
    draw.rectangle(
        [(x, y + cellH - 32), (x + cellW, y + cellH)], fill=(15, 23, 42)
    )
    draw.text(
        (x + 10, y + cellH - 26),
        f"出所: {label}",
        fill=(148, 163, 184),
        font=font_small,
    )

  buf = io.BytesIO()
  img_out.save(buf, format="PNG")
  buf.seek(0)
  return Response(content=buf.getvalue(), media_type="image/png")


# ==========================================
# 3. 画面UI[cite: 1]
# ==========================================


@app.get("/", response_class=HTMLResponse)
def get_scanner_page():
  html_content = r"""
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>フィギュア買取インベントリ</title>
        <script src="https://unpkg.com/html5-qrcode" type="text/javascript"></script>
        <style>
            body { font-family: sans-serif; text-align: center; background: #121212; color: #fff; padding: 10px; margin: 0; }
            h2 { color: #fff; margin-bottom: 2px; font-size: 18px; }
            .box-tag { background: #333; color: #00ffcc; padding: 2px 8px; border-radius: 4px; font-size: 12px; display: inline-block; margin-bottom: 8px; }
            p { color: #aaa; font-size: 12px; margin-bottom: 12px; }
            #reader-container { max-width: 380px; margin: 0 auto 15px auto; background: #000; border-radius: 10px; overflow: hidden; border: 2px solid #00ffcc; display: none; }
            .btn-toggle { background: #00ffcc; color: #000; font-weight: bold; border: none; padding: 14px 20px; border-radius: 8px; cursor: pointer; font-size: 16px; width: 100%; max-width: 380px; margin-bottom: 10px; }
            
            .manual-input-box { max-width: 380px; margin: 0 auto 15px auto; background: #1e1e1e; padding: 10px; border-radius: 8px; border: 1px solid #444; display: flex; gap: 6px; box-sizing: border-box; }
            .manual-input { flex-grow: 1; background: #2a2a2a; color: #fff; border: 1px solid #555; padding: 8px 10px; border-radius: 6px; font-size: 14px; }
            .btn-manual { background: #3498db; color: #fff; border: none; padding: 8px 14px; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 13px; white-space: nowrap; }
            .btn-manual:hover { background: #2980b9; }

            .result-banner { background: #1e3a20; color: #d4edda; border: 1px solid #28a745; max-width: 380px; margin: 10px auto; padding: 10px; border-radius: 8px; text-align: left; display: none; font-size: 13px; }
            .banner-content { display: flex; align-items: center; justify-content: space-between; }
            .banner-item { display: flex; align-items: center; overflow: hidden; }
            .banner-qty-controls { display: flex; align-items: center; gap: 4px; flex-shrink: 0; margin-left: 8px; }
            
            .error { color: #ff6b6b; margin-top: 5px; font-weight: bold; font-size: 13px; }
            .list-section { background: #1e1e1e; color: #fff; max-width: 380px; margin: 15px auto; padding: 12px; border-radius: 8px; text-align: left; }
            .list-section h3 { font-size: 14px; margin-top: 0; border-bottom: 2px solid #333; padding-bottom: 5px; }
            .item-row { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #2c2c2c; padding: 10px 0; font-size: 12px; gap: 8px; }
            .item-thumb { width: 36px; height: 36px; object-fit: cover; background: #fff; border-radius: 4px; flex-shrink: 0; border: 1px solid #444; }
            .item-info { flex-grow: 1; overflow: hidden; }
            .item-title { color: #eee; line-height: 1.4; font-weight: bold; }
            .item-jan { color: #888; font-size: 11px; margin-top: 2px; }
            .item-quote { font-size: 10px; color: #38bdf8; margin-top: 2px; }
            .item-quote a { color: #38bdf8; text-decoration: none; }
            
            .qty-control { display: flex; align-items: center; white-space: nowrap; flex-shrink: 0; gap: 4px; }
            .qty-btn { background: #444; color: white; border: none; width: 24px; height: 24px; border-radius: 4px; cursor: pointer; font-weight: bold; font-size: 13px; display: flex; align-items: center; justify-content: center; }
            .qty-btn:active { background: #666; }
            .qty-val { margin: 0 2px; font-weight: bold; font-size: 12px; min-width: 14px; text-align: center; }
            .img-edit-btn { background: #e67e22; color: white; border: none; border-radius: 4px; padding: 4px 5px; cursor: pointer; font-size: 10px; }
            .img-edit-btn:hover { background: #d35400; }
            .del-btn { background: #cc3333; color: white; border: none; border-radius: 4px; padding: 4px 5px; cursor: pointer; font-size: 10px; }
            
            .summary-box { background: #222; border: 1px solid #444; padding: 10px; border-radius: 6px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; font-size: 14px; }
            .btn { background: #007bff; color: white; border: none; padding: 10px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; width: 100%; margin-top: 8px; font-weight: bold; }
            .btn-export { background: #28a745; }
            .btn-image { background: #ff9900; color: #000; }
            .btn-lashin { background: #e67e22; color: #fff; }
            .btn-google { background: #4285F4; color: #fff; }
            .btn-google:hover { background: #3367D6; }
            .btn-clear { background: transparent; border: 1px solid #666; color: #aaa; font-size: 11px; padding: 3px 8px; border-radius: 4px; cursor: pointer; }
            .btn-clear:hover { background: #442222; color: #ff6666; border-color: #ff6666; }
            
            #modal-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); display: none; justify-content: center; align-items: center; z-index: 1000; }
            .modal-content { background: #222; padding: 20px; border-radius: 8px; width: 90%; max-width: 320px; text-align: left; border: 1px solid #555; max-height: 90vh; overflow-y: auto; }
            .modal-content h3 { margin-top: 0; color: #ffcc00; font-size: 16px; }
            .modal-content label { font-size: 12px; color: #aaa; display: block; margin-top: 8px; }
            .modal-content input[type="text"], .modal-content input[type="file"] { width: 100%; padding: 8px; margin-top: 4px; background: #333; border: 1px solid #555; color: #fff; border-radius: 4px; box-sizing: border-box; font-size: 13px; }
            .modal-btns { display: flex; gap: 8px; margin-top: 15px; }

            #preview-container { display: flex; flex-direction: column; align-items: center; gap: 20px; margin-top: 15px; }
            .preview-card { background: #1e1e1e; padding: 12px; border-radius: 8px; border: 1px solid #444; width: 100%; max-width: 380px; box-sizing: border-box; text-align: left; }
            .preview-img { width: 100%; height: auto; border-radius: 4px; border: 1px solid #555; display: block; }
        </style>
    </head>
    <body>
        <h2>📦 買取インベントリ</h2>
        <div id="box-tag-display" class="box-tag">作業枠: メイン</div>
        <p>カメラにかざすか、バーコードを手打ち入力</p>
        
        <button id="scan-toggle-btn" class="btn-toggle" onclick="toggleScanner()">📷 カメラを起動する</button>
        <div id="reader-container"><div id="interactive" style="width: 100%;"></div></div>

        <div class="manual-input-box">
            <input type="text" id="manual-jan-input" class="manual-input" placeholder="JANコードを手打ち入力..." onkeydown="if(event.key==='Enter') submitManualCode()">
            <button class="btn-manual" onclick="submitManualCode()">追加</button>
        </div>

        <div id="result-banner" class="result-banner">
            <div class="banner-content">
                <div class="banner-item">
                    <img id="res-img" src="" style="width: 48px; height: 48px; object-fit: contain; margin-right: 10px; background: white; border-radius: 4px;">
                    <div style="overflow: hidden;">
                        <div id="res-title" style="font-weight: bold; white-space: nowrap; text-overflow: ellipsis; overflow: hidden; max-width: 190px;"></div>
                        <div id="res-jan" style="color: #aaa; font-size: 11px; margin-top: 2px;"></div>
                        <div id="res-quote" style="font-size: 10px; color: #38bdf8; margin-top: 1px;"></div>
                    </div>
                </div>
                <div class="banner-qty-controls">
                    <button class="qty-btn" onclick="adjustLastScanned(-1)">-</button>
                    <span id="res-count" class="qty-val">1</span>
                    <button class="qty-btn" onclick="adjustLastScanned(1)">+</button>
                </div>
            </div>
        </div>
        <div id="error-msg" class="error"></div>

        <div class="list-section">
            <div class="summary-box">
                <div>登録点数: <strong id="total-items" style="color:#00ffcc; font-size:18px;">0</strong> 点</div>
                <button class="btn-clear" onclick="clearCart()">🗑 リストを空にする</button>
            </div>
            <h3><span>📋 持込買取リスト</span></h3>
            <div id="cart-items"><p style="color: #777; text-align: center; margin: 8px 0;">まだ商品は追加されていません</p></div>
            
            <button class="btn btn-lashin" onclick="sendToLashinbangBox()">🚀 らしんばん買取BOXへ一括追加</button>
            <button class="btn btn-export" onclick="exportList()">📋 テキストリストをコピーする</button>
            <button class="btn btn-image" onclick="generatePreviewsServer()">🖼️ パッケージ写真プレビュー生成</button>
        </div>
        <div id="preview-container"></div>

        <!-- 手動登録・画像再登録・ファイルアップロードモーダル -->
        <div id="modal-overlay">
            <div class="modal-content">
                <h3 id="modal-heading">⚠️ 未登録商品（手動登録）</h3>
                <p id="modal-desc" style="font-size: 11px; color: #ccc; margin-bottom: 8px;">DBに無いコードのため「未登録リスト」に記録しました。</p>
                <button class="btn btn-google" style="margin-top:0; margin-bottom:8px;" onclick="openGoogleSearch()">🔍 GoogleでこのJANを調べる</button>

                <label>JANコード</label>
                <input type="text" id="modal-jan" readonly style="background: #222; color: #888;">
                <label>商品名</label>
                <input type="text" id="modal-title" placeholder="例: フィギュア名など">
                
                <label>画像URL (任意)</label>
                <input type="text" id="modal-image" placeholder="例: 公式サイト等の画像URL">

                <label>または画像ファイルをアップロード</label>
                <input type="file" id="modal-file" accept="image/*">

                <div class="modal-btns">
                    <button class="btn" style="background:#555; margin-top:0;" onclick="closeModal()">キャンセル</button>
                    <button class="btn" style="background:#28a745; margin-top:0;" onclick="submitManualItem()">登録して追加</button>
                </div>
            </div>
        </div>

        <script>
            const urlParams = new URLSearchParams(window.location.search);
            const currentBox = urlParams.get('box') || 'default';
            const STORAGE_KEY = `figure_scanner_cart_${currentBox}`;

            let cart = {};
            let html5QrCode = null;
            let isScanning = false;
            let currentBannerJan = "";
            let lastScannedCode = "";
            let lastScanTime = 0;

            window.addEventListener("DOMContentLoaded", () => {
                if (currentBox !== 'default') {
                    document.getElementById("box-tag-display").innerText = "作業枠 (箱番号): " + currentBox;
                }
                const saved = localStorage.getItem(STORAGE_KEY);
                if (saved) {
                    try {
                        cart = JSON.parse(saved);
                        updateCartUI(false);
                    } catch (e) {
                        cart = {};
                    }
                }
            });

            function saveToStorage() {
                localStorage.setItem(STORAGE_KEY, JSON.stringify(cart));
            }

            window.clearCart = function() {
                if (Object.keys(cart).length === 0) return;
                if (confirm(`この枠（${currentBox}）のリストをすべて消去しますか？`)) {
                    cart = {};
                    saveToStorage();
                    updateCartUI();
                    document.getElementById("result-banner").style.display = "none";
                    document.getElementById("preview-container").innerHTML = "";
                }
            }

            window.toggleScanner = function() {
                const container = document.getElementById("reader-container");
                const btn = document.getElementById("scan-toggle-btn");
                if (!isScanning) {
                    container.style.display = "block";
                    btn.innerText = "⏹️ カメラを停止する";
                    btn.style.background = "#ff4444";
                    isScanning = true;
                    html5QrCode = new Html5Qrcode("interactive");
                    html5QrCode.start(
                        { facingMode: "environment" }, 
                        { fps: 10, qrbox: { width: 250, height: 100 } }, 
                        onScanSuccess, 
                        () => {}
                    ).catch(err => {
                        document.getElementById("error-msg").innerText = "カメラ起動エラー: " + err;
                        isScanning = false;
                        container.style.display = "none";
                        btn.innerText = "📷 カメラを起動する";
                        btn.style.background = "#00ffcc";
                    });
                } else {
                    if (html5QrCode) {
                        html5QrCode.stop().then(() => {
                            container.style.display = "none";
                            btn.innerText = "📷 カメラを起動する";
                            btn.style.background = "#00ffcc";
                            isScanning = false;
                        }).catch(() => {
                            container.style.display = "none";
                            btn.innerText = "📷 カメラを起動する";
                            btn.style.background = "#00ffcc";
                            isScanning = false;
                        });
                    }
                }
            }

            window.submitManualCode = async function() {
                const inputElem = document.getElementById("manual-jan-input");
                const codeVal = inputElem.value.trim();
                if (!codeVal) return;

                inputElem.value = "";
                await processAndAddCode(codeVal);
            }

            async function onScanSuccess(decodedText) {
                const now = Date.now();
                if (decodedText === lastScannedCode && (now - lastScanTime) < 3000) return;
                if ((now - lastScanTime) < 1200) return;

                lastScannedCode = decodedText;
                lastScanTime = now;

                await processAndAddCode(decodedText);
            }

            async function processAndAddCode(codeText) {
                try {
                    document.getElementById("error-msg").innerText = "照合中...";
                    const res = await fetch('/process_jan', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ jan: codeText })
                    });
                    
                    if (!res.ok) {
                        const errData = await res.json().catch(() => ({}));
                        if (res.status === 404) {
                            openManualModal(errData.jan || codeText);
                            document.getElementById("error-msg").innerText = "未登録商品を検出し、未登録リストに記録しました。";
                            return;
                        }
                        document.getElementById("error-msg").innerText = "未対応コード: " + codeText;
                        return;
                    }

                    const data = await res.json();
                    addOrUpdateCartItem(data);
                    document.getElementById("error-msg").innerText = "";

                    if (navigator.vibrate) navigator.vibrate(50);
                } catch (e) {
                    document.getElementById("error-msg").innerText = "通信エラー";
                }
            }

            function addOrUpdateCartItem(data) {
                const code = data.jan;
                currentBannerJan = code;
                
                if (cart[code]) {
                    cart[code].count += 1;
                    if (data.title) cart[code].title = data.title;
                    if (data.image_url) cart[code].imageUrl = data.image_url;
                    if (data.quote_label) cart[code].quote_label = data.quote_label;
                    if (data.source_url) cart[code].source_url = data.source_url;
                } else {
                    cart[code] = { 
                        jan: code, 
                        title: data.title || "", 
                        imageUrl: data.image_url || "", 
                        count: 1,
                        quote_label: data.quote_label || "流通 / 商品カタログ",
                        source_url: data.source_url || "#"
                    };
                }
                updateCartUI();

                document.getElementById("res-title").innerText = cart[code].title;
                document.getElementById("res-jan").innerText = "コード: " + code;
                document.getElementById("res-quote").innerHTML = `出所: <a href="${cart[code].source_url}" target="_blank" rel="noopener" style="color:#38bdf8;text-decoration:none;">${cart[code].quote_label}</a>`;
                document.getElementById("res-count").innerText = cart[code].count;
                document.getElementById("res-img").src = `/proxy_image?jan=${encodeURIComponent(code)}&url=${encodeURIComponent(cart[code].imageUrl || '')}&refresh=1&t=` + Date.now();
                document.getElementById("result-banner").style.display = "block";
            }

            window.openManualModal = function(jan, existingTitle = "", existingImage = "") {
                document.getElementById("modal-jan").value = jan;
                document.getElementById("modal-title").value = existingTitle;
                document.getElementById("modal-image").value = existingImage;
                document.getElementById("modal-file").value = "";
                
                if (existingTitle) {
                    document.getElementById("modal-heading").innerText = "🖼️ 商品画像の再登録 / 修正";
                    document.getElementById("modal-desc").innerText = "画像URLの変更、またはファイルから直接画像をアップロードできます。";
                } else {
                    document.getElementById("modal-heading").innerText = "⚠ 未登録商品（手動登録）";
                    document.getElementById("modal-desc").innerText = "DBに無いコードのため「未登録リスト」に記録しました。";
                }
                document.getElementById("modal-overlay").style.display = "flex";
            }

            window.openEditImageModal = function(code) {
                const item = cart[code];
                if (!item) return;
                openManualModal(code, item.title, item.imageUrl || "");
            }

            window.closeModal = function() {
                document.getElementById("modal-overlay").style.display = "none";
            }

            window.openGoogleSearch = function() {
                const jan = document.getElementById("modal-jan").value;
                const searchUrl = `https://www.google.com/search?q=${encodeURIComponent(jan)}`;
                window.open(searchUrl, '_blank');
            }

            window.submitManualItem = async function() {
                const jan = document.getElementById("modal-jan").value;
                const title = document.getElementById("modal-title").value.trim();
                const imageUrl = document.getElementById("modal-image").value.trim();
                const fileInput = document.getElementById("modal-file");

                if (!title) {
                    alert("商品名を入力してください。");
                    return;
                }

                const formData = new FormData();
                formData.append("jan", jan);
                formData.append("title", title);
                formData.append("price", "買取中!!");
                formData.append("image_url", imageUrl);
                if (fileInput.files.length > 0) {
                    formData.append("file", fileInput.files[0]);
                }

                try {
                    const res = await fetch('/manual_register', {
                        method: 'POST',
                        body: formData
                    });

                    if (!res.ok) throw new Error("登録に失敗しました");

                    const data = await res.json();
                    closeModal();

                    // すでにカートにある場合は個数を増やさず情報だけ上書き更新する
                    if (cart[jan]) {
                        cart[jan].title = data.title;
                        cart[jan].imageUrl = data.image_url;
                        cart[jan].quote_label = data.quote_label;
                        cart[jan].source_url = data.source_url;
                        updateCartUI();
                        
                        currentBannerJan = jan;
                        document.getElementById("res-title").innerText = data.title;
                        document.getElementById("res-jan").innerText = "コード: " + jan;
                        document.getElementById("res-quote").innerHTML = `出所: <a href="${data.source_url}" target="_blank" rel="noopener" style="color:#38bdf8;text-decoration:none;">${data.quote_label}</a>`;
                        document.getElementById("res-count").innerText = cart[jan].count;
                        document.getElementById("res-img").src = `/proxy_image?jan=${encodeURIComponent(jan)}&url=${encodeURIComponent(data.image_url || '')}&refresh=1&t=` + Date.now();
                        document.getElementById("result-banner").style.display = "block";
                    } else {
                        addOrUpdateCartItem(data);
                    }

                    document.getElementById("error-msg").innerText = "画像・情報を更新してリストに反映しました！";
                } catch (err) {
                    alert("エラー: " + err.message);
                }
            }

            window.sendToLashinbangBox = async function() {
                const keys = Object.keys(cart);
                if (keys.length === 0) {
                    alert("リストが空です。先にスキャンまたは入力を行ってください。");
                    return;
                }

                let jansToProcess = [];
                keys.forEach(code => {
                    const count = cart[code].count || 1;
                    for (let i = 0; i < count; i++) {
                        jansToProcess.push(code);
                    }
                });

                if (!confirm(`持込リストの合計 ${jansToProcess.length} 件（アイテム種類数: ${keys.length}件）をらしんばんの買取BOXに一括追加します。よろしいですか？`)) {
                    return;
                }

                alert("サーバーの裏でらしんばんへの自動追加処理を実行中です。完了するまで少しお待ちください。");

                try {
                    const response = await fetch('/api/bulk_add_box', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ jans: jansToProcess })
                    });

                    const data = await response.json();
                    if (response.ok) {
                        alert("✨ すべての商品のらしんばんBOXへの追加が完了しました！");
                        window.open("https://shop.lashinbang.com/kaitori/offer?nattoku=1", "_blank");
                    } else {
                        alert("エラーが発生しました: " + (data.detail || '不明'));
                    }
                } catch (err) {
                    alert("通信エラー: " + err.message);
                }
            }

            window.adjustLastScanned = function(delta) {
                if (currentBannerJan && cart[currentBannerJan]) {
                    changeQty(currentBannerJan, delta);
                }
            }

            window.changeQty = function(code, delta) {
                if (cart[code]) {
                    cart[code].count += delta;
                    if (cart[code].count <= 0) delete cart[code];
                    updateCartUI();
                    if (currentBannerJan === code) {
                        if (cart[code]) {
                            document.getElementById("res-count").innerText = cart[code].count;
                        } else {
                            document.getElementById("result-banner").style.display = "none";
                        }
                    }
                }
            }

            window.removeItem = function(code) {
                const item = cart[code];
                const itemName = item ? item.title : "この商品";
                if (!confirm(`「${itemName}」をリストから削除しますか？`)) {
                    return;
                }

                delete cart[code];
                if (currentBannerJan === code) {
                    document.getElementById("result-banner").style.display = "none";
                }
                updateCartUI();
            }

            function updateCartUI(doSave = true) {
                if (doSave) saveToStorage();
                const container = document.getElementById("cart-items");
                container.innerHTML = "";
                let totalItems = 0;
                const keys = Object.keys(cart);
                if (keys.length === 0) {
                    container.innerHTML = '<p style="color: #777; text-align: center; margin: 8px 0;">まだ商品は追加されていません</p>';
                    document.getElementById("total-items").innerText = "0";
                    return;
                }
                keys.forEach(code => {
                    const item = cart[code];
                    totalItems += item.count;
                    const thumbUrl = `/proxy_image?jan=${encodeURIComponent(code)}&url=${encodeURIComponent(item.imageUrl || '')}&t=` + Date.now();
                    const row = document.createElement("div");
                    row.className = "item-row";
                    row.innerHTML = `
                        <img src="${thumbUrl}" class="item-thumb" onerror="this.style.background='#442222';">
                        <div class="item-info">
                            <div class="item-title">${item.title}</div>
                            <div class="item-jan">コード: ${item.jan}</div>
                            <div class="item-quote">出所: <a href="${item.source_url || '#'}" target="_blank" rel="noopener">${item.quote_label || '流通 / 商品カタログ'}</a></div>
                        </div>
                        <div class="qty-control">
                            <button class="qty-btn" onclick="changeQty('${code}', -1)">-</button>
                            <span class="qty-val">${item.count}</span>
                            <button class="qty-btn" onclick="changeQty('${code}', 1)">+</button>
                            <button class="img-edit-btn" onclick="openEditImageModal('${code}')">画像</button>
                            <button class="del-btn" onclick="removeItem('${code}')">×</button>
                        </div>
                    `;
                    container.appendChild(row);
                });
                document.getElementById("total-items").innerText = totalItems;
            }

            window.exportList = function() {
                const keys = Object.keys(cart);
                if (keys.length === 0) return alert("リストが空です。");
                let text = `【 買取持込リスト 】\\n`, totalItems = 0;
                keys.forEach(code => {
                    const item = cart[code];
                    totalItems += item.count;
                    text += `- ${item.title} : ${item.count}個 [JAN: ${item.jan}]\\n`;
                });
                text += `\\n合計点数: ${totalItems}点\\n`;
                navigator.clipboard.writeText(text).then(() => alert("持込リストをコピーしました！"));
            }

            window.generatePreviewsServer = async function() {
                const keys = Object.keys(cart);
                if (keys.length === 0) {
                    alert("リストが空です。先にスキャンを行ってください。");
                    return;
                }
                const container = document.getElementById("preview-container");
                container.innerHTML = "<p style='color:#00ffcc;'>サーバー側で高精細パッケージシートを生成中...</p>";

                try {
                    const itemsList = keys.map(k => {
                        const item = cart[k];
                        const img = item.imageUrl || item.image_url || "";
                        return {
                            jan: String(item.jan || k),
                            title: String(item.title || ""),
                            count: parseInt(item.count) || 1,
                            image_url: String(img),
                            quote_label: String(item.quote_label || "流通 / 商品カタログ"),
                            source_url: String(item.source_url || "#")
                        };
                    });

                    const chunkSize = 9;
                    const totalPages = Math.ceil(itemsList.length / chunkSize);
                    container.innerHTML = "";

                    for (let page = 0; page < totalPages; page++) {
                        const chunk = itemsList.slice(page * chunkSize, (page + 1) * chunkSize);
                        const payload = { items: chunk, page: page + 1, total_pages: totalPages };
                        
                        const res = await fetch('/generate_sheet', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(payload)
                        });

                        if (!res.ok) {
                            const errData = await res.text();
                            throw new Error("HTTP " + res.status + " : " + errData);
                        }

                        const blob = await res.blob();
                        const imgUrl = URL.createObjectURL(blob);
                        const card = document.createElement("div");
                        card.className = "preview-card";
                        card.innerHTML = `
                            <h3 style="font-size:13px; color:#00ffcc; margin:0 0 8px 0;">シート ${page + 1} / ${totalPages}</h3>
                            <img src="${imgUrl}" class="preview-img">
                            <div style="font-size: 10px; color: #888; margin-top: 5px; text-align: center;">💡 画像を長押し/右クリックで保存</div>
                        `;
                        container.appendChild(card);
                    }
                } catch (e) {
                    container.innerHTML = `<p style='color:#ff6b6b;'>プレビュー生成に失敗: ${e.message}</p>`;
                    alert("プレビュー生成エラー: " + e.message);
                }
            }
        </script>
    </body>
    </html>
    """
  return html_content


@app.post("/process_jan")
def process_jan_code(data: ScanRequest):
  try:
    raw_code = data.jan
    cleanText = "".join(filter(str.isdigit, raw_code))

    if cleanText.startswith("2510") and len(cleanText) > 13:
      cleanText = cleanText[-13:]
    elif len(cleanText) >= 18:
      cleanText = cleanText[-18:]
    elif len(cleanText) >= 13:
      cleanText = cleanText[-13:]
    else:
      raise HTTPException(
          status_code=400,
          detail=f"無効または欠損コードです (13桁未満): {raw_code}",
      )

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
            SELECT jan, title, image_url, price, maker, quote_label, source_url 
            FROM items WHERE jan = ?
        """,
        (cleanText,),
    )
    row = cursor.fetchone()
    conn.close()

    if not row:
      try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO unregistered_logs (jan) VALUES (?)",
            (cleanText,),
        )
        conn.commit()
        conn.close()
      except Exception as db_err:
        print(f"⚠️ ローカル未登録ログ保存スキップ: {db_err}")

      try:
        _, unreg_sheet = get_gspread_sheets()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        unreg_sheet.append_row([cleanText, now_str])
      except Exception as sheet_err:
        print(f"⚠️ スプレッドシート未登録リスト保存スキップ: {sheet_err}")

      return JSONResponse(
          status_code=404,
          content={
              "detail": f"商品名が見つかりませんでした (JAN: {cleanText})",
              "jan": cleanText,
          },
      )

    existing_title = (
        row[1]
        if (row[1] and row[1].strip())
        else f"プライズフィギュア ({cleanText})"
    )
    existing_price = (
        row[3] if (row[3] and row[3].strip()) else "買取中!!"
    )
    existing_image = row[2] if (row[2] and row[2].strip()) else ""

    if row[5]:
      quote_label = row[5]
      source_url = row[6]
    else:
      meta = resolve_quote_meta(existing_title)
      quote_label = meta["quote_label"]
      source_url = meta["source_url"]

    return {
        "source": "sqlite_cache",
        "jan": cleanText,
        "title": existing_title,
        "image_url": existing_image,
        "price": existing_price,
        "quote_label": quote_label,
        "source_url": source_url,
    }
  except Exception as e:
    if isinstance(e, JSONResponse):
      raise e
    if isinstance(e, HTTPException):
      raise e
    raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
  import uvicorn

  port = int(os.environ.get("PORT", 8000))
  uvicorn.run(app, host="0.0.0.0", port=port)
