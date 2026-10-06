# motion-agent（オープニング制作）。設計: Docs/OPENING_MOTION_PLAN.md §4・M0 の実測は §13
# Node（Remotion の描画）と Python（FastAPI・timing・検査・音）を同居させる。
# Alpine は不可（Remotion 公式）。GPU は使わない。
FROM node:22-bookworm-slim

# Chrome Headless Shell の依存ライブラリ（Remotion 公式のリスト）＋ ffmpeg ＋ 日本語・絵文字フォント ＋ Python
# 日本語の書体の束（B4 書体の描き替え・B7 強調）は Debian main のパッケージだけで揃える（外部ダウンロードなし）。
# 一覧と用途の本籍は remotion/src/gimmicks/fonts.json
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl ffmpeg \
      fonts-noto-cjk fonts-noto-cjk-extra fonts-noto-color-emoji \
      fonts-ipaexfont fonts-mplus fonts-vlgothic fonts-motoya-l-cedar fonts-motoya-l-maruberi \
      fonts-kouzan-mouhitsu fonts-aoyagi-soseki fonts-hanazono fonts-misaki fonts-seto fonts-klee \
      fonts-yozvox-yozfont \
      libnss3 libdbus-1-3 libatk1.0-0 libgbm-dev libasound2 libxrandr2 \
      libxkbcommon-dev libxfixes3 libxcomposite1 libxdamage1 libatk-bridge2.0-0 \
      libpango-1.0-0 libcairo2 libcups2 \
      python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Python（Debian の素の pip は使えないので venv）
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Remotion（依存は lock から入れる）
WORKDIR /app/remotion
COPY remotion/package.json remotion/package-lock.json ./
RUN npm ci --no-audit --no-fund
RUN npx remotion browser ensure

# 見本の仮素材（実在の人物・作品を含まない図形）。型の samples/*.input.json が static/samples/ で参照する
RUN mkdir -p public/samples \
 && ffmpeg -y -loglevel error -f lavfi -i "mandelbrot=size=1280x720:rate=1" -frames:v 1 public/samples/img_01.png \
 && ffmpeg -y -loglevel error -f lavfi -i "cellauto=size=1280x720:rule=110:rate=1" -frames:v 1 -vf "negate" public/samples/img_02.png \
 && ffmpeg -y -loglevel error -f lavfi -i "smptehdbars=size=1280x720:rate=1" -frames:v 1 public/samples/img_03.png \
 && ffmpeg -y -loglevel error -f lavfi -i "gradients=size=1280x720:rate=1:seed=7" -frames:v 1 public/samples/img_04.png \
 && ffmpeg -y -loglevel error -f lavfi -i "life=size=640x360:rate=30:mold=10:ratio=0.3:death_color=#c0392b:life_color=#f4f1ea" -t 6 -vf "scale=1280:720:flags=neighbor" -pix_fmt yuv420p public/samples/vid_01.mp4 \
 && ffmpeg -y -loglevel error -f lavfi -i "testsrc2=size=1280x720:rate=30" -t 6 -pix_fmt yuv420p public/samples/vid_02.mp4

COPY remotion/ ./
# バンドルを作っておく（dev で src を差し替えたら render.mjs が更新日時を見て作り直す）
RUN node render.mjs --bundle

WORKDIR /app
COPY app/ ./app/
COPY tests/ ./tests/
# 見本の脳のシルエット（C1 の画像の点群の見本・自作）
RUN python -m app.tools.make_brain

# ユーザーの書体の置き場（shared/motion/fonts）を書体の検索先に加える（app/core/fonts.py）
COPY fonts-user.conf /etc/fonts/conf.d/99-motion-user-fonts.conf

EXPOSE 8007
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8007"]
