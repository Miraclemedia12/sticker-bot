FROM python:3.11-slim

# ffmpeg is needed to turn videos/GIFs into moving stickers
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libwebp-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
