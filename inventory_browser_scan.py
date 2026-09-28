import io
import json
import os
import sqlite3
import urllib.parse
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel
import requests

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

# 💡 クラウド用: 環境変数 GCP_CREDENTIALS_JSON があれば credentials.json を自動生成
if not os.path.exists(credential_path):
  gcp_json_env = os.environ.get("GCP_CREDENTIALS_JSON")
  if gcp_json_env:
    with open(credential_path, "w", encoding="utf-8") as f:
      f.write(gcp_json_env)


def init_db():
  conn = sqlite3.connect(db_path)
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS items (
            jan TEXT PRIMARY KEY,
            title TEXT,
            image_url TEXT,
            price TEXT
        )
    """)
  conn.commit()
  conn.close()


init_db()


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

  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
      ),
      "Referer": "https://www.neatzanime.cz/",
  }
  if target_url and target_url.startswith("/"):
    target_url = "https://www.pasomaru.co.jp" + target_url

  res = None
  if target_url:
    try:
      res = requests.get(target_url, headers=headers, timeout=2.5)
    except Exception:
      res = None

  if (not res or res.status_code != 200) and jan:
    fixed_url = search_neatz_fallback(jan)
    if fixed_url:
      try:
        res = requests.get(fixed_url, headers=headers, timeout=2.5)
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
    with open(local_file, "wb") as f:
      f.write(res.content)
    return res.content

  return None


def sync_sheet_to_local():
  if not os.path.exists(credential_path):
    print(
        "⚠️ credentials.json"
        " が見つかりません。環境変数(GCP_CREDENTIALS_JSON)を確認してください。"
    )
    return
  try:
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
    rows = sheet.get_all_values()

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

        cursor.execute(
            """
                INSERT OR REPLACE INTO items (jan, title, image_url, price)
                VALUES (?, ?, ?, ?)
            """,
            (jan, title, image_url, price),
        )
        count += 1

    conn.commit()
    conn.close()
    print(
        f"🚀 スプレッドシートから {count} 件のデータをローカルDBへ高速同期しました！"
    )
  except Exception as e:
    print(f"⚠️ スプレッドシート同期スキップ: {e}")


sync_sheet_to_local()


@app.get("/proxy_image")
def proxy_image(url: str = None, jan: str = None):
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


class SheetRenderRequest(BaseModel):
  items: list[CartItem]
  page: int
  total_pages: int


@app.post("/generate_sheet")
def generate_sheet(req: SheetRenderRequest):
  print(
      f"\n🎨 [サーバー画像生成] シート {req.page}/{req.total_pages} (商品数:"
      f" {len(req.items)}件)"
  )
  canvas_w, canvas_h = 900, 1100
  img_out = Image.new("RGB", (canvas_w, canvas_h), color=(30, 30, 30))
  draw = ImageDraw.Draw(img_out)

  # Docker(Linux)およびWindowsローカルフォントの両対応
  font_large = None
  font_badge = None
  font_small = None
  font_paths = [
      "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
      "/usr/share/fonts/truetype/ipafont-gothic/ipag.ttf",
      "C:\\Windows\\Fonts\\msgothic.ttc",
      "msgothic.ttc",
  ]
  for fp in font_paths:
    if os.path.exists(fp):
      try:
        font_large = ImageFont.truetype(fp, 28)
        font_badge = ImageFont.truetype(fp, 56)
        font_small = ImageFont.truetype(fp, 14)
        break
      except Exception:
        continue

  if not font_large:
    font_large = ImageFont.load_default()
    font_badge = ImageFont.load_default()
    font_small = ImageFont.load_default()

  header_text = (
      f"📦 買取パッケージ一覧 (シート {req.page} / {req.total_pages})"
  )
  draw.text((40, 40), header_text, fill=(0, 255, 204), font=font_large)

  cols = 3
  startX, startY = 40, 100
  cellW, cellH = 260, 290
  gapX, gapY = 30, 30

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
        scale = max(cellW / tile_w, cellH / tile_h)
        nw, nh = int(tile_w * scale), int(tile_h * scale)
        tile = tile.resize((nw, nh), Image.Resampling.BILINEAR)

        crop_x = (nw - cellW) // 2
        crop_y = (nh - cellH) // 2
        tile = tile.crop((crop_x, crop_y, crop_x + cellW, crop_y + cellH))
        img_out.paste(tile, (x, y))
      except Exception as e:
        print(f"  └ ⚠️ タイル合成エラー: {e}")
    else:
      draw.text(
          (x + 75, y + 100), "No Image", fill=(255, 204, 0), font=font_large
      )
      title_display = (
          item.title[:14] + "..." if len(item.title) > 14 else item.title
      )
      draw.text(
          (x + 15, y + 150), title_display, fill=(255, 255, 255), font=font_small
      )

    draw.rectangle([x, y, x + cellW, y + cellH], outline=(80, 80, 80), width=2)

    badge = f"{item.count}個"
    draw.rectangle(
        [
            x + cellW // 2 - 60,
            y + cellH // 2 - 35,
            x + cellW // 2 + 60,
            y + cellH // 2 + 35,
        ],
        fill=(0, 0, 0, 180),
    )
    draw.text(
        (x + cellW // 2 - 45, y + cellH // 2 - 30),
        badge,
        fill=(0, 255, 102),
        font=font_badge,
    )

  buf = io.BytesIO()
  img_out.save(buf, format="PNG")
  buf.seek(0)
  print(f"  └ ✅ [シート生成完了] 返却します\n")
  return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/", response_class=HTMLResponse)
def get_scanner_page():
  html_content = """
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
            .btn-toggle { background: #00ffcc; color: #000; font-weight: bold; border: none; padding: 14px 20px; border-radius: 8px; cursor: pointer; font-size: 16px; width: 100%; max-width: 380px; margin-bottom: 15px; }
            
            .result-banner { background: #1e3a20; color: #d4edda; border: 1px solid #28a745; max-width: 380px; margin: 10px auto; padding: 10px; border-radius: 8px; text-align: left; display: none; font-size: 13px; }
            .banner-content { display: flex; align-items: center; justify-content: space-between; }
            .banner-item { display: flex; align-items: center; overflow: hidden; }
            .banner-qty-controls { display: flex; align-items: center; gap: 4px; flex-shrink: 0; margin-left: 8px; }
            
            .error { color: #ff6b6b; margin-top: 5px; font-weight: bold; font-size: 13px; }
            .list-section { background: #1e1e1e; color: #fff; max-width: 380px; margin: 15px auto; padding: 12px; border-radius: 8px; text-align: left; }
            .list-section h3 { font-size: 14px; margin-top: 0; border-bottom: 2px solid #333; padding-bottom: 5px; }
            .item-row { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #2c2c2c; padding: 10px 0; font-size: 12px; gap: 8px; }
            .item-info { flex-grow: 1; overflow: hidden; }
            .item-title { color: #eee; line-height: 1.4; font-weight: bold; }
            .item-jan { color: #888; font-size: 11px; margin-top: 2px; }
            
            .qty-control { display: flex; align-items: center; white-space: nowrap; flex-shrink: 0; }
            .qty-btn { background: #444; color: white; border: none; width: 26px; height: 26px; border-radius: 4px; cursor: pointer; font-weight: bold; font-size: 14px; display: flex; align-items: center; justify-content: center; }
            .qty-btn:active { background: #666; }
            .qty-val { margin: 0 6px; font-weight: bold; font-size: 13px; min-width: 16px; text-align: center; }
            .del-btn { background: #cc3333; color: white; border: none; border-radius: 4px; padding: 4px 8px; margin-left: 6px; cursor: pointer; font-size: 11px; }
            
            .summary-box { background: #222; border: 1px solid #444; padding: 10px; border-radius: 6px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; font-size: 14px; }
            .btn { background: #007bff; color: white; border: none; padding: 10px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; width: 100%; margin-top: 8px; font-weight: bold; }
            .btn-export { background: #28a745; }
            .btn-image { background: #ff9900; color: #000; }
            .btn-clear { background: transparent; border: 1px solid #666; color: #aaa; font-size: 11px; padding: 3px 8px; border-radius: 4px; cursor: pointer; }
            .btn-clear:hover { background: #442222; color: #ff6666; border-color: #ff6666; }
            
            #preview-container { display: flex; flex-direction: column; align-items: center; gap: 20px; margin-top: 15px; }
            .preview-card { background: #1e1e1e; padding: 12px; border-radius: 8px; border: 1px solid #444; width: 100%; max-width: 380px; box-sizing: border-box; text-align: left; }
            .preview-img { width: 100%; height: auto; border-radius: 4px; border: 1px solid #555; display: block; }
        </style>
    </head>
    <body>
        <h2>📦 買取インベントリ</h2>
        <div id="box-tag-display" class="box-tag">作業枠: メイン</div>
        <p>カメラにかざすだけで自動連続読み取り</p>
        <button id="scan-toggle-btn" class="btn-toggle" onclick="toggleScanner()">📷 カメラを起動する</button>
        <div id="reader-container"><div id="interactive" style="width: 100%;"></div></div>

        <!-- スキャン結果バナー -->
        <div id="result-banner" class="result-banner">
            <div class="banner-content">
                <div class="banner-item">
                    <img id="res-img" src="" style="width: 48px; height: 48px; object-fit: contain; margin-right: 10px; background: white; border-radius: 4px;">
                    <div style="overflow: hidden;">
                        <div id="res-title" style="font-weight: bold; white-space: nowrap; text-overflow: ellipsis; overflow: hidden; max-width: 190px;"></div>
                        <div id="res-jan" style="color: #aaa; font-size: 11px; margin-top: 2px;"></div>
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
                <button class="btn-clear" onclick="clearCart()">🗑️ リストを空にする</button>
            </div>
            <h3>
                <span>📋 持込買取リスト</span>
            </h3>
            <div id="cart-items"><p style="color: #777; text-align: center; margin: 8px 0;">まだ商品は追加されていません</p></div>
            <button class="btn btn-export" onclick="exportList()">📋 テキストリストをコピーする</button>
            <button class="btn btn-image" onclick="generatePreviewsServer()">🖼️ パッケージ写真プレビュー生成</button>
        </div>
        <div id="preview-container"></div>

        <script>
            // 💡 URLの ?box=〇〇 パラメータで作業枠を分離
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

            function clearCart() {
                if (Object.keys(cart).length === 0) return;
                if (confirm(`この枠（${currentBox}）のリストをすべて消去しますか？`)) {
                    cart = {};
                    saveToStorage();
                    updateCartUI();
                    document.getElementById("result-banner").style.display = "none";
                    document.getElementById("preview-container").innerHTML = "";
                }
            }

            function toggleScanner() {
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
                    );
                } else {
                    if (html5QrCode) {
                        html5QrCode.stop().then(() => {
                            container.style.display = "none";
                            btn.innerText = "📷 カメラを起動する";
                            btn.style.background = "#00ffcc";
                            isScanning = false;
                        });
                    }
                }
            }

            async function onScanSuccess(decodedText) {
                const now = Date.now();

                if (decodedText === lastScannedCode && (now - lastScanTime) < 3000) return;
                if ((now - lastScanTime) < 1200) return;

                lastScannedCode = decodedText;
                lastScanTime = now;

                try {
                    document.getElementById("error-msg").innerText = "照合中...";
                    const res = await fetch('/process_jan', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ jan: decodedText })
                    });
                    
                    if (!res.ok) {
                        document.getElementById("error-msg").innerText = "未対応コード/短縮コード無効: " + decodedText;
                        return;
                    }

                    const data = await res.json();
                    const code = data.jan;
                    currentBannerJan = code;
                    
                    if (cart[code]) {
                        cart[code].count += 1;
                    } else {
                        cart[code] = { 
                            jan: code, 
                            title: data.title || "", 
                            imageUrl: data.image_url || "", 
                            count: 1 
                        };
                    }
                    updateCartUI();

                    document.getElementById("res-title").innerText = data.title;
                    document.getElementById("res-jan").innerText = "コード: " + code;
                    document.getElementById("res-count").innerText = cart[code].count;
                    document.getElementById("res-img").src = `/proxy_image?jan=${encodeURIComponent(code)}&url=${encodeURIComponent(data.image_url || '')}`;
                    document.getElementById("result-banner").style.display = "block";
                    document.getElementById("error-msg").innerText = "";

                    if (navigator.vibrate) navigator.vibrate(50);
                } catch (e) {
                    document.getElementById("error-msg").innerText = "通信エラーが発生しました";
                }
            }

            function adjustLastScanned(delta) {
                if (currentBannerJan && cart[currentBannerJan]) {
                    changeQty(currentBannerJan, delta);
                }
            }

            function changeQty(code, delta) {
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

            function removeItem(code) {
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
                    const row = document.createElement("div");
                    row.className = "item-row";
                    row.innerHTML = `
                        <div class="item-info">
                            <div class="item-title">${item.title}</div>
                            <div class="item-jan">コード: ${item.jan}</div>
                        </div>
                        <div class="qty-control">
                            <button class="qty-btn" onclick="changeQty('${code}', -1)">-</button>
                            <span class="qty-val">${item.count}</span>
                            <button class="qty-btn" onclick="changeQty('${code}', 1)">+</button>
                            <button class="del-btn" onclick="removeItem('${code}')">×</button>
                        </div>
                    `;
                    container.appendChild(row);
                });
                document.getElementById("total-items").innerText = totalItems;
            }

            function exportList() {
                const keys = Object.keys(cart);
                if (keys.length === 0) return alert("リストが空です。");
                let text = `【 買取持込リスト (${currentBox}) 】\\n`, totalItems = 0;
                keys.forEach(code => {
                    const item = cart[code];
                    totalItems += item.count;
                    text += `- ${item.title} : ${item.count}個 [JAN: ${item.jan}]\\n`;
                });
                text += `\\n合計点数: ${totalItems}点\\n`;
                navigator.clipboard.writeText(text).then(() => alert("持込リスト（点数一覧）をコピーしました！"));
            }

            async function generatePreviewsServer() {
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
                            image_url: String(img)
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
                    console.error("プレビュー生成例外:", e);
                    container.innerHTML = `<p style='color:#ff6b6b;'>プレビュー生成に失敗しました: ${e.message}</p>`;
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
        "SELECT jan, title, image_url, price FROM items WHERE jan = ?",
        (cleanText,),
    )
    row = cursor.fetchone()
    conn.close()

    existing_title = (
        row[1] if (row and row[1] and row[1].strip()) else f"プライズフィギュア ({cleanText})"
    )
    existing_price = row[3] if (row and row[3] and row[3].strip()) else "買取中!!"
    existing_image = row[2] if (row and row[2] and row[2].strip()) else ""

    print(f"📥 [スキャン即答] JAN: {cleanText}")
    return {
        "source": "sqlite_cache" if row else "unregistered",
        "jan": cleanText,
        "title": existing_title,
        "image_url": existing_image,
        "price": existing_price,
    }
  except Exception as e:
    if isinstance(e, HTTPException):
      raise e
    raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
  import uvicorn

  port = int(os.environ.get("PORT", 8000))
  uvicorn.run(app, host="0.0.0.0", port=port)