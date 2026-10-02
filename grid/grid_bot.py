"""SalemGrid - paper (virtual) spot-grid bot using live Binance prices.

No API key needed: prices come from Binance public data. No real orders are placed.
Settings live in grid/params.json (updated by the daily review). When the version
changes, the bot closes its virtual positions and rebuilds the grids with the new settings.
"""
import csv, json, os, pickle, time, traceback
from datetime import datetime, timezone, timedelta

import ccxt
import requests

from grid_core import Grid

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.getenv("DATA_DIR", "/data")
STATE = os.path.join(DATA, "grid_state.pkl")
FILLS = os.path.join(DATA, "grid_fills.csv")
TG_TOKEN, TG_CHAT = os.getenv("TG_TOKEN", ""), os.getenv("TG_CHAT_ID", "")
TG_EACH_TRADE = os.getenv("GRID_TG_TRADES", "off") == "on"   # 20+ trades/day -> off by default
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


def load_params():
    with open(os.path.join(HERE, "params.json")) as f:
        return json.load(f)


def make_grids(params, prices, capital):
    each = capital / len(params["pairs"])
    return {p: Grid(each, prices[p], step=params["step"], half_levels=params["half_levels"],
                    stop_below=params["stop_below"]) for p in params["pairs"]}


def save(state):
    tmp = STATE + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(state, f)
    os.replace(tmp, STATE)


def record(pair, fill, equity, version):
    new = not os.path.exists(FILLS)
    with open(FILLS, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "pair", "side", "price", "qty", "pnl_usdt", "reason",
                        "pair_equity", "params_version"])
        ts, side, price, qty, pnl, reason = fill
        w.writerow([datetime.fromtimestamp(ts, RIYADH).isoformat(timespec="seconds"), pair, side,
                    f"{price:.6f}", f"{qty:.8f}", f"{pnl:.4f}", reason, f"{equity:.2f}", version])


def total_equity(state, prices):
    return sum(g.equity(prices[p]) for p, g in state["grids"].items())


def main():
    os.makedirs(DATA, exist_ok=True)
    ex = ccxt.binance({"enableRateLimit": True})
    params = load_params()
    state = pickle.load(open(STATE, "rb")) if os.path.exists(STATE) else None
    pairs = set(params["pairs"]) | (set(state["grids"]) if state else set())
    prices = {p: ex.fetch_ticker(p)["last"] for p in pairs}

    if state is None:
        state = {"grids": make_grids(params, prices, params["capital"]), "version": params["version"],
                 "start_capital": params["capital"], "day": datetime.now(RIYADH).date(),
                 "day_start_equity": params["capital"], "day_trades": 0}
        log(f"new grids v{params['version']} on {params['pairs']} with {params['capital']} USDT")
        tg(f"🟢 SalemGrid started (virtual {params['capital']} USDT), settings v{params['version']}")
    elif state["version"] != params["version"]:
        eq = 0.0
        for p, g in state["grids"].items():       # close virtual positions at market
            before = len(g.fills)
            g._sell_all(prices[p], time.time(), "new-settings")
            for fill in g.fills[before:]:
                record(p, fill, g.cash, state["version"])
            eq += g.cash
        state["grids"] = make_grids(params, prices, eq)
        state["version"] = params["version"]
        log(f"switched to settings v{params['version']} {params}, equity {eq:.2f}")
        tg(f"🔧 SalemGrid: new settings v{params['version']} applied (step {params['step']*100:.1f}%, "
           f"{2*params['half_levels']} levels, pairs {', '.join(params['pairs'])}). Equity {eq:.2f} USDT")
    else:
        log(f"resumed v{state['version']}")
    seen = {p: len(g.fills) for p, g in state["grids"].items()}

    while True:
        try:
            now = time.time()
            for p, g in state["grids"].items():
                price = ex.fetch_ticker(p)["last"]
                prices[p] = price
                g.on_price(price, now)
                for fill in g.fills[seen[p]:]:
                    record(p, fill, g.equity(price), state["version"])
                    _, side, fp, qty, pnl, reason = fill
                    msg = f"{side} {p} @ {fp:.4f} ({reason})" + (f" pnl {pnl:+.3f}" if side == "SELL" else "")
                    log(msg)
                    if side == "SELL":
                        state["day_trades"] += 1
                    if TG_EACH_TRADE or reason == "stop":
                        tg(("⚠️ " if reason == "stop" else "") + msg)
                seen[p] = len(g.fills)
            today = datetime.now(RIYADH).date()
            if today != state["day"]:                # end of the Riyadh day -> summary
                eq = total_equity(state, prices)
                day_pnl = eq - state["day_start_equity"]
                s = (f"📊 SalemGrid {state['day']}: {state['day_trades']} completed trades, "
                     f"day {day_pnl:+.2f} USDT, equity {eq:.2f} USDT "
                     f"({(eq / state['start_capital'] - 1) * 100:+.2f}% total), settings v{state['version']}")
                log(s)
                tg(s)
                state.update(day=today, day_start_equity=eq, day_trades=0)
            save(state)
        except Exception:
            log("error: " + traceback.format_exc().splitlines()[-1])
        time.sleep(5)


if __name__ == "__main__":
    main()
