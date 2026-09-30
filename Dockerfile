FROM python:3.10-slim

# 日本語フォント（IPAフォント）およびPlaywright/Chromiumに必要なシステムライブラリのインストール
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-ipafont-gothic \
    libgl1 \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libgobject-2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwrightのブラウザ（Chromium）をコンテナ内にインストール
RUN playwright install chromium

COPY . .

# Render等で割り当てられるポートに対応
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn inventory_browser_scan:app --host 0.0.0.0 --port ${PORT:-8000}"]
