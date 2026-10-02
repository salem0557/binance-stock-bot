"""SalemGrid - paper (virtual) spot-grid bot using live Binance prices.

No API key needed: prices come from Binance public data. No real orders are placed.
Settings (Railway variables, all optional):
  GRID_PAIRS    BTC/USDT,ETH/USDT   coins to trade (capital is split equally)
  GRID_CAPITAL  200                 virtual USDT
  GRID_LEVELS   16                  grid lines per coin
  GRID_RANGE    0.15                grid covers +-15% around the start price
  GRID_TRAIL    0                   e.g. 0.005 = wait for 0.5% pullback before selling
  GRID_CRASH    0                   e.g. 0.05 = stop buying after a 5% drop in 4h
"""
import csv, os, pickle, time, traceback
from datetime import datetime, timezone, timedelta

import ccxt
import requests

from grid_core import Grid

DATA = os.getenv("DATA_DIR", "/data")
STATE = os.path.join(DATA, "grid_state.pkl")
FILLS = os.path.join(DATA, "grid_fills.csv")
PAIRS = [p.strip() for p in os.getenv("GRID_PAIRS", "BTC/USDT,ETH/USDT").split(",") if p.strip()]
CAPITAL = float(os.getenv("GRID_CAPITAL", "200"))
LEVELS = int(os.getenv("GRID_LEVELS", "16"))
RANGE = float(os.getenv("GRID_RANGE", "0.15"))
TRAIL = float(os.getenv("GRID_TRAIL", "0"))
CRASH = float(os.getenv("GRID_CRASH", "0")) or 9.0   # 9.0 = never triggers
TG_TOKEN, TG_CHAT = os.getenv("TG_TOKEN", ""), os.getenv("TG_CHAT_ID", "")
RIYADH = timezone(timedelta(hours=3))


def log(msg):
    print(f"[GRID] {datetime.now(RIYADH):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": msg}, timeout=10)
        except Exception:
            pass


def new_grid(pair, price, capital):
    return Grid(capital, price, rng=RANGE, n=LEVELS, trail=TRAIL, crash=CRASH)


def save(state):
    tmp = STATE + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(state, f)
    os.replace(tmp, STATE)


def record(pair, fill, equity):
    new = not os.path.exists(FILLS)
    with open(FILLS, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "pair", "side", "price", "qty", "pnl_usdt", "pair_equity"])
        ts, side, price, qty, pnl = fill
        w.writerow([datetime.fromtimestamp(ts, RIYADH).isoformat(timespec="seconds"),
                    pair, side, f"{price:.6f}", f"{qty:.8f}", f"{pnl:.4f}", f"{equity:.2f}"])


def summary(state, prices):
    eq = sum(g.equity(prices[p]) for p, g in state["grids"].items())
    lines = [f"📊 SalemGrid (virtual)  equity: {eq:.2f} USDT  "
             f"({(eq / state['start_capital'] - 1) * 100:+.2f}%)  refills: {state['refills']}"]
    for p, g in state["grids"].items():
        sells = sum(1 for f in g.fills if f[1] == "SELL")
        lines.append(f"{p}: {g.equity(prices[p]):.2f}  realized {g.realized:+.2f}  sells {sells}")
    return "\n".join(lines)


def main():
    os.makedirs(DATA, exist_ok=True)
    ex = ccxt.binance({"enableRateLimit": True})
    prices = {p: ex.fetch_ticker(p)["last"] for p in PAIRS}
    if os.path.exists(STATE):
        state = pickle.load(open(STATE, "rb"))
        log(f"resumed state, refills={state['refills']}")
    else:
        each = CAPITAL / len(PAIRS)
        state = {"grids": {p: new_grid(p, prices[p], each) for p in PAIRS},
                 "start_capital": CAPITAL, "refills": 0, "last_summary": time.time()}
        log(f"new grids on {PAIRS} with {CAPITAL} USDT")
        tg(f"🟢 SalemGrid started (virtual {CAPITAL:.0f} USDT) on {', '.join(PAIRS)}")
    seen = {p: len(g.fills) for p, g in state["grids"].items()}
    last = dict(prices)

    while True:
        try:
            now = time.time()
            for p, g in state["grids"].items():
                price = ex.fetch_ticker(p)["last"]
                g.on_price(last[p], price, now)
                last[p] = price
                for fill in g.fills[seen[p]:]:
                    eq = g.equity(price)
                    record(p, fill, eq)
                    _, side, fp, qty, pnl = fill
                    msg = f"{'🟢 BUY ' if side == 'BUY' else '🔴 SELL'} {p} @ {fp:.4f}"
                    if side == "SELL":
                        msg += f"  pnl {pnl:+.3f} USDT"
                    log(msg)
                    tg(msg)
                seen[p] = len(g.fills)
                # lost (almost) everything -> refill virtual balance and keep learning
                each = state["start_capital"] / len(state["grids"])
                if g.equity(price) < each * 0.1:
                    state["refills"] += 1
                    state["grids"][p] = new_grid(p, price, each)
                    seen[p] = len(state["grids"][p].fills)
                    tg(f"⚠️ {p} grid lost its balance - refilled to {each:.0f} USDT (refill #{state['refills']})")
            if now - state["last_summary"] > 24 * 3600:
                s = summary(state, last)
                log(s.replace("\n", " | "))
                tg(s)
                state["last_summary"] = now
            save(state)
        except Exception:
            log("error: " + traceback.format_exc().splitlines()[-1])
        time.sleep(10)


if __name__ == "__main__":
    main()
