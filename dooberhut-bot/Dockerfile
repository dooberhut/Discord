# Railway deploys Dockerfiles out of the box
FROM python:3.11-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App
COPY . .

# Railway will set env vars; we rely on DISCORD_TOKEN
CMD ["python", "bot.py"]
