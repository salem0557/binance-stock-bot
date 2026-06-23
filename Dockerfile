# Railway / root build for the bStocks trading bot.
# The bot lives in bot/, but Railway builds from the repo root — so this
# root Dockerfile builds bot/ without needing a custom "Root Directory".
FROM python:3.11-slim

WORKDIR /app
COPY bot/requirements.txt ./bot/requirements.txt
RUN pip install --no-cache-dir -r bot/requirements.txt

COPY bot /app/bot
WORKDIR /app/bot

# Unbuffered logs so the platform shows activity in real time.
ENV PYTHONUNBUFFERED=1
# Railway injects PORT automatically; the bot serves its web monitor on it.
EXPOSE 8000
CMD ["python", "trading_bot.py"]
