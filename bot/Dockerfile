# 24/7 tokenized-US-stocks (bStocks) trading bot image.
# Build:  docker build -t stockbot ./bot
# Run:    docker run --env-file bot/.env -v "$PWD/bot":/app/bot stockbot
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/bot
WORKDIR /app/bot

# Unbuffered logs so `docker logs` shows activity in real time.
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "trading_bot.py"]
