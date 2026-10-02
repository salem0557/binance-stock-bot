#!/bin/bash
# MODE: backtest | dryrun | live
set -e
MODE="${MODE:-dryrun}"
STRAT="${STRATEGY:-SalemTrend}"
CFG=/freqtrade/user_data/config.json

# Persistent storage (Railway volume mounted at /data)
DATA_DIR=/freqtrade/user_data
if [ -d /data ]; then
  sudo -n /bin/chown -R ftuser:ftuser /data 2>/dev/null || true
  if touch /data/.w 2>/dev/null; then DATA_DIR=/data; else echo "WARNING: /data not writable, using temporary storage"; fi
fi
mkdir -p "$DATA_DIR/data"

# Secrets from Railway variables -> temporary config (never stored in GitHub)
python3 - <<'PY'
import json, os
tg = os.getenv("TG_TOKEN", "")
s = {
  "exchange": {"key": os.getenv("BINANCE_KEY", ""), "secret": os.getenv("BINANCE_SECRET", "")},
  "telegram": {"enabled": bool(tg), "token": tg, "chat_id": os.getenv("TG_CHAT_ID", "")},
}
json.dump(s, open("/tmp/secrets.json", "w"))
PY

echo "=== MODE: $MODE | STRATEGY: $STRAT ==="

if [ "$MODE" = "backtest" ]; then
  TR="${TIMERANGE:-20220601-}"
  # Download extra history before the test start so indicators are ready
  freqtrade download-data -c "$CFG" --datadir "$DATA_DIR/data" -t 4h 1d --timerange "${DL_FROM:-20220101}-"
  freqtrade backtesting -c "$CFG" --datadir "$DATA_DIR/data" -s "$STRAT" --timerange "$TR" --export none --enable-protections
  echo "=== BACKTEST DONE - change MODE to dryrun when ready ==="
  sleep infinity
elif [ "$MODE" = "dryrun" ]; then
  exec freqtrade trade -c "$CFG" -c /tmp/secrets.json -s "$STRAT" \
    --datadir "$DATA_DIR/data" --db-url "sqlite:///$DATA_DIR/dryrun.sqlite" --dry-run
elif [ "$MODE" = "live" ]; then
  export FREQTRADE__DRY_RUN=false
  exec freqtrade trade -c "$CFG" -c /tmp/secrets.json -s "$STRAT" \
    --datadir "$DATA_DIR/data" --db-url "sqlite:///$DATA_DIR/live.sqlite"
else
  echo "Unknown MODE=$MODE (use backtest, dryrun or live)"; sleep infinity
fi
