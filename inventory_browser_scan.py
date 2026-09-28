import os
import sqlite3
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from pydantic import BaseModel
import requests

app = FastAPI(title="Figure Inventory Browser Scan Edition")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

base_dir = os.path.dirname(os.path.abspath(__file__))
credential_path = os.path.join(base_dir, "credentials.json")
db_path = os.path.join(base_dir, "inventory.db")


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


def sync_sheet_to_local():
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
    print(f"🚀 スプレッドシートから {count} 件のデータをローカルDBへ同期しました！")
  except Exception as e:
    print(f"⚠️ スプレッドシート同期スキップ: {e}")


sync_sheet_to_local()


class ScanRequest(BaseModel):
  jan: str


@app.get("/", response_class=HTMLResponse)
def get_scanner_page():
  """ブラウザ側リアルタイム高速スキャン画面"""
  html_content = """
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>フィギュア買取インベントリ（ブラウザ高速スキャン）</title>
        <!-- HTML5-QRCode ライブラリの読み込み（スマホカメラで爆速デコード） -->
        <script src="https://unpkg.com/html5-qrcode" type="text/javascript"></script>
        <style>
            body { font-family: sans-serif; text-align: center; background: #121212; color: #fff; padding: 10px; margin: 0; }
            h2 { color: #fff; margin-bottom: 2px; font-size: 18px; }
            p { color: #aaa; font-size: 12px; margin-bottom: 12px; }
            
            #reader-container { max-width: 350px; margin: 0 auto 15px auto; background: #000; border-radius: 10px; overflow: hidden; border: 2px solid #00ffcc; display: none; }
            
            .btn-toggle { background: #00ffcc; color: #000; font-weight: bold; border: none; padding: 14px 20px; border-radius: 8px; cursor: pointer; font-size: 16px; width: 100%; max-width: 350px; margin-bottom: 15px; box-shadow: 0 4px 10px rgba(0,255,204,0.3); }
            .btn-toggle:active { background: #00ccaa; }
            
            .result-banner { background: #1e4620; color: #d4edda; border: 1px solid #28a745; max-width: 350px; margin: 10px auto; padding: 10px; border-radius: 6px; text-align: left; display: none; font-size: 13px; }
            .error { color: #ff6b6b; margin-top: 5px; font-weight: bold; font-size: 13px; }
            
            .list-section { background: #1e1e1e; color: #fff; max-width: 350px; margin: 15px auto; padding: 12px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.5); text-align: left; }
            .list-section h3 { font-size: 14px; margin-top: 0; border-bottom: 2px solid #333; padding-bottom: 5px; display: flex; justify-content: space-between; align-items: center; }
            .item-row { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #2c2c2c; padding: 6px 0; font-size: 12px; }
            .item-info { flex-grow: 1; margin-right: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #ddd; }
            .item-price { color: #ff9966; font-weight: bold; font-size: 11px; margin-right: 6px; }
            .item-count { font-weight: bold; background: #333; color: #fff; padding: 2px 6px; border-radius: 4px; }
            
            .summary-box { background: #222; border: 1px solid #444; padding: 8px; border-radius: 6px; margin-bottom: 10px; display: flex; justify-content: space-between; font-size: 13px; }
            
            .btn { background: #007bff; color: white; border: none; padding: 8px 12px; border-radius: 4px; cursor: pointer; font-size: 13px; width: 100%; margin-top: 8px; }
            .btn-export { background: #28a745; }
        </style>
    </head>
    <body>

        <h2>📦 買取インベントリ (高速スキャン)</h2>
        <p>カメラにかざすだけで自動連続読み取り</p>

        <button id="scan-toggle-btn" class="btn-toggle" onclick="toggleScanner()">📷 カメラを起動する</button>
        <div id="reader-container">
            <div id="interactive" style="width: 100%;"></div>
        </div>

        <div id="result-banner" class="result-banner">
            <div style="display: flex; align-items: center;">
                <img id="res-img" src="" style="width: 50px; height: 50px; object-fit: contain; margin-right: 10px; background: white; border-radius: 4px;">
                <div style="overflow: hidden;">
                    <div id="res-title" style="font-weight: bold; white-space: nowrap; text-overflow: ellipsis; overflow: hidden;"></div>
                    <div style="color: #ff9966; font-weight: bold;"><span id="res-price"></span></div>
                </div>
            </div>
        </div>
        <div id="error-msg" class="error"></div>

        <div class="list-section">
            <div class="summary-box">
                <div>点数: <strong id="total-items">0</strong>点</div>
                <div>合計金額: <strong id="total-amount" style="color:#00ffcc;">0円</strong></div>
            </div>
            <h3>
                <span>📋 持込買取リスト</span>
            </h3>
            <div id="cart-items">
                <p style="color: #777; text-align: center; margin: 8px 0;">まだ商品は追加されていません</p>
            </div>
            <button class="btn btn-export" onclick="exportList()">リストをテキストで書き出す</button>
        </div>

        <script>
            let cart = {};
            let html5QrCode = null;
            let isScanning = false;

            function toggleScanner() {
                const container = document.getElementById("reader-container");
                const btn = document.getElementById("scan-toggle-btn");

                if (!isScanning) {
                    container.style.display = "block";
                    btn.innerText = "⏹️ カメラを停止する";
                    btn.style.background = "#ff4444";
                    isScanning = true;

                    html5QrCode = new Html5Qrcode("interactive");
                    const config = { fps: 10, qrbox: { width: 250, height: 100 } };
                    
                    html5QrCode.start(
                        { facingMode: "environment" }, 
                        config, 
                        onScanSuccess, 
                        onScanFailure
                    ).catch(err => {
                        document.getElementById("error-msg").innerText = "カメラの起動に失敗しました: " + err;
                        toggleScanner();
                    });
                } else {
                    if (html5QrCode) {
                        html5QrCode.stop().then(() => {
                            container.style.display = "none";
                            btn.innerText = "📷 カメラを起動する";
                            btn.style.background = "#00ffcc";
                            isScanning = false;
                        }).catch(err => { console.log(err); });
                    }
                }
            }

            let lastScannedCode = "";
            let lastScanTime = 0;

            function onScanSuccess(decodedText, decodedResult) {
                // 連続で同じバーコードを何重にも読み込まないためのガード（2秒間隔）
                const now = Date.now();
                if (decodedText === lastScannedCode && (now - lastScanTime) < 2000) {
                    return;
                }
                lastScannedCode = decodedText;
                lastScanTime = now;

                document.getElementById("error-msg").innerText = "商品を検索中...";

                // サーバーへJANコードのみを送信
                fetch('/process_jan', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ jan: decodedText })
                })
                .then(async response => {
                    const data = await response.json();
                    if (!response.ok) {
                        throw new Error(data.detail || "商品が見つかりませんでした");
                    }
                    return data;
                })
                .then(data => {
                    const code = data.jan;
                    const numericPrice = parseInt(data.price.replace(/[^0-9]/g, '')) || 0;

                    if (cart[code]) {
                        cart[code].count += 1;
                    } else {
                        cart[code] = {
                            title: data.title,
                            priceStr: data.price,
                            priceNum: numericPrice,
                            count: 1
                        };
                    }
                    updateCartUI();

                    document.getElementById("res-title").innerText = data.title;
                    document.getElementById("res-img").src = data.image_url;
                    document.getElementById("res-price").innerText = data.price;
                    document.getElementById("result-banner").style.display = "block";
                    document.getElementById("error-msg").innerText = "";

                    if (navigator.vibrate) { navigator.vibrate(50); }
                })
                .catch(error => {
                    document.getElementById("error-msg").innerText = error.message;
                });
            }

            function onScanFailure(error) {
                // 読み取り試行中のエラーは無視（毎フレーム発生するため）
            }

            function updateCartUI() {
                const container = document.getElementById("cart-items");
                container.innerHTML = "";
                
                let totalItems = 0;
                let grandTotal = 0;
                const keys = Object.keys(cart);
                
                if (keys.length === 0) {
                    container.innerHTML = '<p style="color: #777; text-align: center; margin: 8px 0;">まだ商品は追加されていません</p>';
                    document.getElementById("total-items").innerText = "0";
                    document.getElementById("total-amount").innerText = "0円";
                    return;
                }

                keys.forEach(code => {
                    const item = cart[code];
                    totalItems += item.count;
                    grandTotal += (item.priceNum * item.count);
                    
                    const row = document.createElement("div");
                    row.className = "item-row";
                    row.innerHTML = `
                        <div class="item-info" title="${item.title}">${item.title}</div>
                        <div style="white-space: nowrap; display:flex; align-items:center;">
                            <span class="item-price">${item.priceStr}</span>
                            <span class="item-count">${item.count}個</span>
                            <button onclick="removeItem('${code}')" style="background:#cc3333; color:white; border:none; border-radius:3px; padding:2px 5px; margin-left:4px; cursor:pointer;">×</button>
                        </div>
                    `;
                    container.appendChild(row);
                });

                document.getElementById("total-items").innerText = totalItems;
                document.getElementById("total-amount").innerText = grandTotal.toLocaleString() + "円";
            }

            function removeItem(code) {
                delete cart[code];
                updateCartUI();
            }

            function exportList() {
                const keys = Object.keys(cart);
                if (keys.length === 0) {
                    alert("リストが空です。");
                    return;
                }

                let text = "【 買取持込リスト 】\\n";
                let grandTotal = 0;
                keys.forEach(code => {
                    const item = cart[code];
                    const subtotal = item.priceNum * item.count;
                    grandTotal += subtotal;
                    text += `- ${item.title} : ${item.count}個 (${item.priceStr}×${item.count} = ${subtotal.toLocaleString()}円) [JAN: ${code}]\\n`;
                });
                text += `\\n合計点数: ${document.getElementById("total-items").innerText}点\\n`;
                text += `合計金額: ${grandTotal.toLocaleString()}円\\n`;

                navigator.clipboard.writeText(text).then(() => {
                    alert("持込リストと金額合計をクリップボードにコピーしました！");
                }).catch(err => {
                    alert("コピーに失敗しました。");
                });
            }
        </script>
    </body>
    </html>
    """
  return html_content


def search_pasomaru_live(jan: str):
  try:
    url = "https://www.pasomaru.co.jp/products"
    params = {"keyword": jan}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
    }

    response = requests.get(url, params=params, headers=headers, timeout=5)
    response.encoding = response.apparent_encoding
    soup = BeautifulSoup(response.text, "html.parser")

    product_row = soup.find("div", class_="products-row")
    if not product_row:
      return None

    header_div = product_row.find("div", class_="products-row-header")
    title = header_div.text.strip() if header_div else f"商品 ({jan})"

    img_tag = product_row.find("img", class_="products-image")
    image_url = img_tag["src"] if img_tag else ""

    price_div = product_row.find("div", class_="products-price-value")
    price = price_div.text.strip() if price_div else "価格不明"

    return {"title": title, "image_url": image_url, "price": price}
  except Exception as e:
    print(f"❌ ライブ検索エラー: {e}")
    return None


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
    elif len(cleanText) >= 8:
      cleanText = cleanText[-8:]
    else:
      raise HTTPException(
          status_code=400, detail=f"無効なコードです: {raw_code}"
      )

    # 1. ローカルSQLiteキャッシュ検索
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT jan, title, image_url, price FROM items WHERE jan = ?",
        (cleanText,),
    )
    row = cursor.fetchone()
    conn.close()

    if row:
      return {
          "source": "sqlite_cache",
          "jan": row[0],
          "title": row[1],
          "image_url": row[2],
          "price": row[3],
      }

    # 2. 未登録ならライブスクレイピング
    scraped = search_pasomaru_live(cleanText)
    if not scraped:
      raise HTTPException(
          status_code=404,
          detail=f"商品情報が見つかりませんでした (JAN: {cleanText})",
      )

    # 3. ローカルSQLiteに保存
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
            INSERT OR REPLACE INTO items (jan, title, image_url, price)
            VALUES (?, ?, ?, ?)
        """,
        (cleanText, scraped["title"], scraped["image_url"], scraped["price"]),
    )
    conn.commit()
    conn.close()

    # 4. スプレッドシートへ自動バックアップ
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
      sheet.append_row(
          [
              cleanText,
              scraped["title"],
              scraped["image_url"],
              scraped["price"],
          ]
      )
    except Exception as e:
      print(f"⚠️ スプレッドシートへの追加スキップ: {e}")

    return {
        "source": "scraped_and_saved_locally",
        "jan": cleanText,
        "title": scraped["title"],
        "image_url": scraped["image_url"],
        "price": scraped["price"],
    }

  except Exception as e:
    if isinstance(e, HTTPException):
      raise e
    raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
  import uvicorn

  uvicorn.run(app, host="0.0.0.0", port=8000)