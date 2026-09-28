import io
import math
import os
import re
import sqlite3
import urllib.parse
from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from PIL import Image, ImageDraw, ImageFont
import requests

app = FastAPI(title="Figure Inventory & Appraisal Scanner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "inventory.db"

# ==========================================
# 1. データベース初期化
# ==========================================


def init_db():
  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS items (
            jan TEXT PRIMARY KEY,
            title TEXT,
            price INTEGER DEFAULT 0,
            image_url TEXT,
            maker TEXT DEFAULT '公式・流通',
            quote_label TEXT DEFAULT '公式出所',
            source_url TEXT DEFAULT '',
            custom_image BLOB,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
  cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_jan ON items (jan)")
  cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_title ON items (title)")
  conn.commit()
  conn.close()


init_db()

# ==========================================
# 2. 公式引用元（テイ）の自動判定エンジン
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
          "wcf",
          "masterlise",
      ]
  ):
    return {
        "maker": "BANDAI SPIRITS",
        "quote_label": "バンプレナビ (公式引用)",
        "source_url": "https://bsp-prize.jp/",
    }
  if any(
      k in t
      for k in [
          "セガ",
          "sega",
          "luminasta",
          "ちょこのせ",
          "tip'n'pop",
          "premium figure",
          "spm",
          "スーパープレミアム",
      ]
  ):
    return {
        "maker": "SEGA",
        "quote_label": "セガプラザ (公式引用)",
        "source_url": "https://segaplaza.jp/",
    }
  if any(
      k in t
      for k in [
          "タイトー",
          "taito",
          "amp+",
          "coreful",
          "aqua float",
          "desktop cute",
          "プチエット",
      ]
  ):
    return {
        "maker": "タイトー",
        "quote_label": "タイトープライズ (公式引用)",
        "source_url": "https://www.taito.co.jp/taito-prize",
    }
  if any(
      k in t
      for k in [
          "フリュー",
          "furyu",
          "bicute",
          "trio-try-it",
          "tenitol",
          "ぬーどるストッパー",
          "ひっかけフィギュア",
      ]
  ):
    return {
        "maker": "フリュー",
        "quote_label": "キャラ広場 (公式引用)",
        "source_url": "https://charahiroba.com/prize/",
    }

  encoded = urllib.parse.quote(title)
  return {
      "maker": "流通公式",
      "quote_label": "商品カタログ詳細",
      "source_url": f"https://www.google.com/search?q={encoded}",
  }


# ==========================================
# 3. 外部フォールバック検索（DB未登録時の自動スクレイピング救済）
# ==========================================


def fetch_external_fallback(code: str):
  """DBにない未登録コードがスキャンされた際、外部プライズDB（Neatzanime等）から即座にタイトル・画像を取得"""
  try:
    url = f"https://neatzanime.com/search?q={code}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
    }
    res = requests.get(url, headers=headers, timeout=4.0)
    if res.status_code == 200:
      # HTMLから簡易正規表現でタイトルと画像を抽出
      title_match = re.search(r'<h3 class="product-title">(.*?)</h3>', res.text)
      img_match = re.search(
          r'<img[^>]+class="product-image"[^>]+src="([^">]+)"', res.text
      )

      if title_match:
        title = title_match.group(1).strip()
        img_url = img_match.group(1).strip() if img_match else ""
        meta = resolve_quote_meta(title)

        # 取得できたデータを即座にローカルDBへキャッシュ登録
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            """
                    INSERT INTO items (jan, title, price, image_url, maker, quote_label, source_url)
                    VALUES (?, ?, 0, ?, ?, ?, ?)
                    ON CONFLICT(jan) DO UPDATE SET title=excluded.title, image_url=excluded.image_url
                """,
            (
                code,
                title,
                img_url,
                meta["maker"],
                meta["quote_label"],
                meta["source_url"],
            ),
        )
        conn.commit()
        conn.close()

        return {
            "jan": code,
            "title": title,
            "price": 0,
            "image_url": (
                f"/api/image-proxy?url={urllib.parse.quote(img_url)}"
                if img_url
                else ""
            ),
            "maker": meta["maker"],
            "quote_label": meta["quote_label"],
            "source_url": meta["source_url"],
            "has_custom": False,
        }
  except Exception as e:
    print(f"Fallback scrape error: {e}")

  return None


# ==========================================
# 4. APIエンドポイント
# ==========================================


@app.get("/api/lookup")
def api_lookup(code: str = Query(...)):
  clean_code = re.sub(r"[^\w]", "", code)
  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()

  # 1. 完全一致
  cursor.execute(
      """
        SELECT jan, title, price, image_url, maker, quote_label, source_url,
               CASE WHEN custom_image IS NOT NULL THEN 1 ELSE 0 END as has_custom
        FROM items WHERE jan = ?
    """,
      (clean_code,),
  )
  row = cursor.fetchone()

  # 2. 前方/後方/部分一致（JAIAコードや余分なプレフィックス対策）
  if not row and len(clean_code) >= 6:
    cursor.execute(
        """
            SELECT jan, title, price, image_url, maker, quote_label, source_url,
                   CASE WHEN custom_image IS NOT NULL THEN 1 ELSE 0 END as has_custom
            FROM items WHERE jan LIKE ? OR ? LIKE '%' || jan || '%' LIMIT 1
        """,
        (f"%{clean_code}%", clean_code),
    )
    row = cursor.fetchone()

  conn.close()

  if row:
    has_custom = bool(row[7])
    display_img = (
        f"/api/custom-image?jan={row[0]}"
        if has_custom
        else (
            f"/api/image-proxy?url={urllib.parse.quote(row[3])}"
            if row[3]
            else ""
        )
    )

    return {
        "status": "success",
        "found": True,
        "data": {
            "jan": row[0],
            "title": row[1],
            "price": row[2],
            "image_url": display_img,
            "maker": row[4] or "公式流通",
            "quote_label": row[5] or "公式引用",
            "source_url": row[6] or "#",
            "has_custom": has_custom,
        },
    }

  # 3. DBに無ければ外部スクレイピング救済を試行
  fallback_result = fetch_external_fallback(clean_code)
  if fallback_result:
    return {"status": "success", "found": True, "data": fallback_result}

  # 4. それでもヒットしない場合
  return {
      "status": "success",
      "found": False,
      "data": {
          "jan": clean_code,
          "title": "未登録商品（店頭未登録・新製品）",
          "price": 0,
          "image_url": "",
          "maker": "不明",
          "quote_label": "手動登録",
          "source_url": "#",
          "has_custom": False,
      },
  }


@app.post("/api/upload-photo")
async def api_upload_photo(request: Request):
  data = await request.json()
  jan = data.get("jan")
  image_base64 = data.get("image_base64")
  if not jan or not image_base64:
    return JSONResponse(status_code=400, content={"error": "パラメータ不足"})

  if "," in image_base64:
    image_base64 = image_base64.split(",", 1)[1]

  import base64

  image_bytes = base64.b64decode(image_base64)

  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()
  cursor.execute(
      """
        INSERT INTO items (jan, title, custom_image) VALUES (?, '現場撮影フィギュア', ?)
        ON CONFLICT(jan) DO UPDATE SET custom_image = excluded.custom_image
    """,
      (jan, image_bytes),
  )
  conn.commit()
  conn.close()
  return {"status": "success", "image_url": f"/api/custom-image?jan={jan}"}


@app.get("/api/custom-image")
def get_custom_image(jan: str):
  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()
  cursor.execute("SELECT custom_image FROM items WHERE jan = ?", (jan,))
  row = cursor.fetchone()
  conn.close()
  if row and row[0]:
    return Response(content=row[0], media_type="image/jpeg")
  return Response(status_code=404)


@app.get("/api/image-proxy")
def image_proxy(url: str):
  if not url:
    return Response(status_code=404)
  try:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
    }
    r = requests.get(url, headers=headers, timeout=5.0)
    return Response(
        content=r.content,
        media_type=r.headers.get("Content-Type", "image/jpeg"),
    )
  except Exception:
    return Response(status_code=502)


# ==========================================
# 5. 持込照合シート生成
# ==========================================


def load_font(size):
  font_paths = [
      "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
      "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
      "C:/Windows/Fonts/msgothic.ttc",
      "C:/Windows/Fonts/meiryo.ttc",
  ]
  for p in font_paths:
    if os.path.exists(p):
      try:
        return ImageFont.truetype(p, size)
      except Exception:
        continue
  return ImageFont.load_default()


@app.post("/api/generate-sheet")
async def generate_sheet(request: Request):
  data = await request.json()
  items = data.get("items", [])
  if not items:
    return JSONResponse(status_code=400, content={"error": "リストが空です"})

  cols = 3
  cell_w, cell_h = 360, 480
  rows = math.ceil(len(items) / cols)
  canvas_w = cols * cell_w
  canvas_h = rows * cell_h + 100

  canvas = Image.new("RGB", (canvas_w, canvas_h), "#F8FAFC")
  draw = ImageDraw.Draw(canvas)

  font_title = load_font(28)
  font_name = load_font(18)
  font_badge = load_font(24)
  font_meta = load_font(13)

  draw.rectangle([(0, 0), (canvas_w, 80)], fill="#0F172A")
  draw.text(
      (24, 24),
      "持込買取・照合用 パッケージ一覧シート",
      font=font_title,
      fill="#FFFFFF",
  )

  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()

  for idx, item_data in enumerate(items):
    c = idx % cols
    r = idx // cols
    x = c * cell_w
    y = 90 + r * cell_h

    draw.rectangle(
        [(x + 10, y + 10), (x + cell_w - 10, y + cell_h - 10)],
        fill="#FFFFFF",
        outline="#E2E8F0",
        width=2,
    )

    cursor.execute(
        """
            SELECT title, image_url, maker, quote_label, custom_image 
            FROM items WHERE jan = ?
        """,
        (item_data["jan"],),
    )
    db_item = cursor.fetchone()

    title = db_item[0] if db_item else "未登録景品"
    raw_img = db_item[1] if db_item else ""
    quote_label = db_item[3] if db_item else "引用情報なし"
    custom_img_bytes = db_item[4] if db_item else None

    img_obj = None
    if custom_img_bytes:
      try:
        img_obj = Image.open(io.BytesIO(custom_img_bytes)).convert("RGB")
      except Exception:
        pass
    elif raw_img:
      try:
        res = requests.get(raw_img, timeout=3.0)
        if res.status_code == 200:
          img_obj = Image.open(io.BytesIO(res.content)).convert("RGB")
      except Exception:
        pass

    if img_obj:
      img_obj.thumbnail((cell_w - 40, cell_h - 170))
      offset_x = x + (cell_w - img_obj.width) // 2
      offset_y = y + 20 + ((cell_h - 170) - img_obj.height) // 2
      canvas.paste(img_obj, (offset_x, offset_y))
    else:
      draw.rectangle(
          [(x + 20, y + 20), (x + cell_w - 20, y + cell_h - 150)],
          fill="#F1F5F9",
      )
      draw.text(
          (x + 70, y + 140), "NO IMAGE", font=font_name, fill="#94A3B8"
      )

    qty = item_data.get("qty", 1)
    draw.rectangle([(x + 20, y + 20), (x + 110, y + 65)], fill="#EF4444")
    draw.text((x + 30, y + 26), f"{qty} 個", font=font_badge, fill="#FFFFFF")

    t_short = title[:34] + ("..." if len(title) > 34 else "")
    draw.text((x + 20, y + cell_h - 130), t_short[:17], font=font_name, fill="#1E293B")
    if len(t_short) > 17:
      draw.text(
          (x + 20, y + cell_h - 105), t_short[17:], font=font_name, fill="#1E293B"
      )

    draw.text(
        (x + 20, y + cell_h - 60),
        f"コード: {item_data['jan']}",
        font=font_meta,
        fill="#64748B",
    )
    draw.text(
        (x + 20, y + cell_h - 40),
        f"出所: {quote_label}",
        font=font_meta,
        fill="#94A3B8",
    )

  conn.close()

  buf = io.BytesIO()
  canvas.save(buf, format="PNG")
  return Response(content=buf.getvalue(), media_type="image/png")


# ==========================================
# 6. モバイルUI
# ==========================================


@app.get("/", response_class=HTMLResponse)
def index_view():
  return """
<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>プライズフィギュア高速持込スキャナー</title>
  <script src="https://cdn.jsdelivr.net/npm/@zxing/library@0.21.3/umd/index.min.js"></script>
  <style>
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0F172A; color: #F8FAFC; }
    header { background: #1E293B; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #334155; }
    h1 { font-size: 16px; margin: 0; font-weight: 700; color: #38BDF8; }
    .badge { background: #0284C7; font-size: 11px; padding: 3px 8px; border-radius: 99px; }
    
    #scanner-container { position: relative; width: 100%; height: 260px; background: #000; overflow: hidden; }
    video { width: 100%; height: 100%; object-fit: cover; }
    .scan-laser { position: absolute; top: 50%; left: 10%; right: 10%; height: 2px; background: #38BDF8; box-shadow: 0 0 8px #38BDF8; animation: laser 1.5s infinite alternate; }
    @keyframes laser { from { top: 25%; } to { top: 75%; } }

    .counter-bar { display: flex; justify-content: space-around; background: #1E293B; padding: 10px; border-bottom: 1px solid #334155; }
    .metric { text-align: center; }
    .metric-val { font-size: 22px; font-weight: 800; color: #38BDF8; }
    .metric-lbl { font-size: 10px; color: #94A3B8; }

    #list-container { padding: 12px; max-height: calc(100vh - 460px); overflow-y: auto; }
    .card { background: #1E293B; border-radius: 10px; padding: 10px; margin-bottom: 8px; display: flex; gap: 10px; align-items: center; border: 1px solid #334155; }
    .card img { width: 64px; height: 64px; object-fit: cover; border-radius: 6px; background: #334155; }
    .card-info { flex: 1; min-width: 0; }
    .card-title { font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .card-meta { font-size: 11px; color: #94A3B8; margin-top: 2px; }
    .card-quote { font-size: 10px; color: #64748B; margin-top: 2px; }
    .card-quote a { color: #38BDF8; text-decoration: none; }
    .card-qty { background: #0284C7; font-size: 16px; font-weight: 700; padding: 4px 10px; border-radius: 8px; }

    .bottom-bar { position: fixed; bottom: 0; left: 0; right: 0; background: #1E293B; padding: 12px 16px; display: flex; gap: 10px; border-top: 1px solid #334155; }
    .btn { flex: 1; padding: 14px; border: none; border-radius: 8px; font-weight: 700; font-size: 14px; cursor: pointer; text-align: center; }
    .btn-sheet { background: #0284C7; color: #FFF; }
    .btn-clear { background: #334155; color: #94A3B8; flex: 0.3; }

    #photo-modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.85); z-index: 100; flex-direction: column; align-items: center; justify-content: center; padding: 20px; }
    #modal-video { width: 100%; max-width: 360px; height: 360px; object-fit: cover; border-radius: 12px; }
  </style>
</head>
<body>
  <header>
    <h1>📦 査定・持込スキャナー</h1>
    <span class="badge" id="box-label">BOX: MAIN</span>
  </header>

  <div id="scanner-container">
    <video id="preview" playsinline></video>
    <div class="scan-laser"></div>
  </div>

  <div class="counter-bar">
    <div class="metric"><div class="metric-val" id="total-qty">0</div><div class="metric-lbl">スキャン総数</div></div>
    <div class="metric"><div class="metric-val" id="unique-count">0</div><div class="metric-lbl">景品種別</div></div>
    <div class="metric"><div class="metric-val" id="total-val">¥0</div><div class="metric-lbl">目安査定額</div></div>
  </div>

  <div id="list-container"></div>

  <div class="bottom-bar">
    <button class="btn btn-clear" onclick="clearList()">リセット</button>
    <button class="btn btn-sheet" onclick="requestSheet()">🖼️ 照合用画像シート生成</button>
  </div>

  <div id="photo-modal">
    <h3 style="margin-top:0;font-size:16px;">📸 実物パッケージを撮影</h3>
    <video id="modal-video" playsinline></video>
    <div style="display:flex;gap:12px;margin-top:16px;width:100%;max-width:360px;">
      <button class="btn btn-clear" onclick="closePhotoModal()">キャンセル</button>
      <button class="btn btn-sheet" onclick="capturePhoto()">撮影して確定</button>
    </div>
  </div>

  <script>
    const urlParams = new URLSearchParams(window.location.search);
    const boxId = urlParams.get('box') || 'main';
    document.getElementById('box-label').innerText = 'BOX: ' + boxId.toUpperCase();

    let scannedItems = {};
    let lastCode = '';
    let lastScanTime = 0;
    let codeReader = null;
    let currentPhotoJan = null;

    async function startScanner() {
      try {
        codeReader = new ZXing.BrowserMultiFormatReader();
        const devices = await codeReader.listVideoInputDevices();
        const backCam = devices.find(d => d.label.toLowerCase().includes('back') || d.label.toLowerCase().includes('背面')) || devices[0];
        
        await codeReader.decodeFromVideoDevice(backCam ? backCam.deviceId : undefined, 'preview', (result, err) => {
          if (result) {
            const now = Date.now();
            const text = result.getText();
            if (text === lastCode && (now - lastScanTime) < 1800) return;
            lastCode = text;
            lastScanTime = now;
            handleScanned(text);
          }
        });
      } catch (err) {
        console.error("Camera init error:", err);
      }
    }

    async function handleScanned(code) {
      if (navigator.vibrate) navigator.vibrate(80);
      
      if (scannedItems[code]) {
        scannedItems[code].qty += 1;
        updateUI();
        return;
      }

      try {
        const res = await fetch(`/api/lookup?code=${encodeURIComponent(code)}`);
        const json = await res.json();
        const d = json.data;

        scannedItems[code] = {
          qty: 1,
          title: d.title,
          price: d.price || 0,
          image_url: d.image_url || '',
          quote_label: d.quote_label || '公式引用',
          source_url: d.source_url || '#'
        };
        updateUI();
      } catch (e) {
        console.error("Lookup error:", e);
      }
    }

    function updateUI() {
      const container = document.getElementById('list-container');
      container.innerHTML = '';
      
      let totalQty = 0;
      let totalVal = 0;
      const keys = Object.keys(scannedItems);

      keys.reverse().forEach(jan => {
        const item = scannedItems[jan];
        totalQty += item.qty;
        totalVal += (item.price * item.qty);

        const card = document.createElement('div');
        card.className = 'card';
        card.innerHTML = `
          <img src="${item.image_url || 'https://via.placeholder.com/64x64/334155/FFFFFF?text=NO+IMG'}" 
               onerror="this.src='https://via.placeholder.com/64x64/334155/FFFFFF?text=NO+IMG'"
               onclick="openPhotoModal('${jan}')">
          <div class="card-info">
            <div class="card-title">${item.title}</div>
            <div class="card-meta">コード: ${jan} | 目安: ¥${item.price.toLocaleString()}</div>
            <div class="card-quote">出所: <a href="${item.source_url}" target="_blank" rel="noopener">${item.quote_label}</a></div>
          </div>
          <div class="card-qty">${item.qty}</div>
        `;
        container.appendChild(card);
      });

      document.getElementById('total-qty').innerText = totalQty;
      document.getElementById('unique-count').innerText = keys.length;
      document.getElementById('total-val').innerText = '¥' + totalVal.toLocaleString();
    }

    function clearList() {
      if (confirm('リストをクリアしますか？')) {
        scannedItems = {};
        updateUI();
      }
    }

    async function requestSheet() {
      const payload = Object.keys(scannedItems).map(jan => ({
        jan: jan,
        qty: scannedItems[jan].qty
      }));
      if (payload.length === 0) return alert('商品がスキャンされていません');

      const btn = document.querySelector('.btn-sheet');
      btn.innerText = '生成中...';
      btn.disabled = true;

      try {
        const res = await fetch('/api/generate-sheet', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ items: payload })
        });
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        window.open(url, '_blank');
      } catch (e) {
        alert('シート生成に失敗しました');
      } finally {
        btn.innerText = '🖼️ 照合用画像シート生成';
        btn.disabled = false;
      }
    }

    let modalStream = null;
    async function openPhotoModal(jan) {
      currentPhotoJan = jan;
      const modal = document.getElementById('photo-modal');
      modal.style.display = 'flex';
      const video = document.getElementById('modal-video');
      modalStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
      video.srcObject = modalStream;
      video.play();
    }

    function closePhotoModal() {
      if (modalStream) modalStream.getTracks().forEach(t => t.stop());
      document.getElementById('photo-modal').style.display = 'none';
    }

    async function capturePhoto() {
      const video = document.getElementById('modal-video');
      const canvas = document.createElement('canvas');
      canvas.width = video.videoWidth || 640;
      canvas.height = video.videoHeight || 640;
      canvas.getContext('2d').drawImage(video, 0, 0);
      const b64 = canvas.toDataURL('image/jpeg', 0.8);

      closePhotoModal();
      
      const res = await fetch('/api/upload-photo', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ jan: currentPhotoJan, image_base64: b64 })
      });
      const json = await res.json();
      
      if (scannedItems[currentPhotoJan]) {
        scannedItems[currentPhotoJan].image_url = json.image_url;
        updateUI();
      }
    }

    window.addEventListener('load', startScanner);
  </script>
</body>
</html>
  """


if __name__ == "__main__":
  import uvicorn

  uvicorn.run(app, host="0.0.0.0", port=10000)
