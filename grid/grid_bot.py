"""SalemGrid - adaptive paper (virtual) grid bot, learns from its own live trades.

No API key needed: live prices come from Binance public data. No real orders are placed.

Every LEARN_SECS (1 hour) the bot looks only at what happened since the last check:
  - scores each coin by its own profit/loss in the last hour (recent hours count more)
  - moves money from losing coins to winning coins (each coin between 4% and 20%)
  - resizes each coin's grid step to how much that coin is moving right now
  - logs the lesson to /data/grid_lessons.csv and Telegram
If total balance drops below 10% of the start, it refills to the start amount and keeps going.

Railway variables (optional): GRID_CAPITAL (2000), GRID_PAIRS (10 coins), GRID_LEARN_SECS (3600),
GRID_TG_HOURLY (on), GRID_TG_TRADES (off), GRID=off disables the bot.
"""
import csv, math, os, pickle, time, traceback
from collections import deque
from datetime import datetime, timezone, timedelta

import ccxt
import requests

from grid_core import Grid

DATA = os.getenv("DATA_DIR", "/data")
STATE = os.path.join(DATA, "grid_state_v3.pkl")
FILLS = os.path.join(DATA, "grid_fills.csv")
LESSONS = os.path.join(DATA, "grid_lessons.csv")
PAIRS = [p.strip() for p in os.getenv(
    "GRID_PAIRS", "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,"
                  "DOGE/USDT,ADA/USDT,AVAX/USDT,LINK/USDT,LTC/USDT").split(",") if p.strip()]
CAPITAL = float(os.getenv("GRID_CAPITAL", "2000"))
LEARN_SECS = int(os.getenv("GRID_LEARN_SECS", "3600"))
TG_TOKEN, TG_CHAT = os.getenv("TG_TOKEN", ""), os.getenv("TG_CHAT_ID", "")
TG_HOURLY = os.getenv("GRID_TG_HOURLY", "on") == "on"
TG_TRADES = os.getenv("GRID_TG_TRADES", "off") == "on"
RIYADH = timezone(timedelta(hours=3))

START_STEP = 0.005                  # before the bot has seen any live movement
STEP_MIN, STEP_MAX = 0.003, 0.015   # 0.3% is the smallest step that still beats the fees
W_MIN, W_MAX = 0.04, 0.20           # each coin gets 4%..20% of the money
STOP_BELOW = 0.05                   # price 5% under the grid -> cut and re-centre lower
REBUILD_DIFF = 0.25                 # rebuild a coin only if step or money changes > 25%


def log(msg):
    print(f"[GRID] {datetime.now(RIYADH):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": msg}, timeout=10)
        except Exception:
            pass


def append_csv(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def short(p):
    return p.split("/")[0]


def half_levels(alloc):
    return max(3, min(8, int(alloc / (2 * 6))))      # keep each order >= ~6 USDT


def new_grid(alloc, price, step):
    return Grid(alloc, price, step=step, half_levels=half_levels(alloc), stop_below=STOP_BELOW)


def fresh_state(prices, capital, refills=0):
    each = capital / len(PAIRS)
    now = time.time()
    return {
        "grids": {p: new_grid(each, prices[p], START_STEP) for p in PAIRS},
        "steps": {p: START_STEP for p in PAIRS},
        "scores": {p: 0.0 for p in PAIRS},
        "hist": {p: deque(maxlen=1500) for p in PAIRS},
        "bank": 0.0, "start_capital": capital, "refills": refills,
        "hour_t": now, "hour_E": capital, "hour_eq": {p: each for p in PAIRS}, "hour_trades": {p: 0 for p in PAIRS},
        "hours": 0, "good_hours": 0,
        "day": datetime.now(RIYADH).date(), "day_eq": capital, "day_trades": 0,
    }


def total_equity(st, prices):
    return st["bank"] + sum(g.equity(prices[p]) for p, g in st["grids"].items())


def live_step(hist, now):
    """Typical 15-minute move over the last hour -> grid step."""
    pts = [(t, px) for t, px in hist if t >= now - 3600]
    if len(pts) < 200:
        return None
    moves, j = [], 0
    for t, px in pts:
        while pts[j][0] < t - 900:
            j += 1
        if t - pts[0][0] >= 900:
            moves.append(abs(px / pts[j][1] - 1))
    if not moves:
        return None
    moves.sort()
    step = moves[len(moves) // 2] * 0.8
    return round(min(STEP_MAX, max(STEP_MIN, step)) * 2000) / 2000   # round to 0.05%


def clamp_weights(raw):
    """w_i = clip(k * raw_i, W_MIN, W_MAX) with k chosen so the weights sum to 1."""
    lo, hi = -60.0, 60.0                       # search log(k)
    for _ in range(200):
        mid = (lo + hi) / 2
        total = sum(min(W_MAX, max(W_MIN, math.exp(mid) * v)) for v in raw.values())
        lo, hi = (mid, hi) if total < 1 else (lo, mid)
    k = math.exp((lo + hi) / 2)
    w = {p: min(W_MAX, max(W_MIN, k * v)) for p, v in raw.items()}
    s = sum(w.values())
    return {p: v / s for p, v in w.items()}


def learn(st, prices, now):
    E = total_equity(st, prices)
    rets, notes = {}, []
    for p, g in st["grids"].items():
        eq = g.equity(prices[p])
        base = st["hour_eq"].get(p) or eq
        rets[p] = (eq / base - 1) * 100 if base else 0.0
        st["scores"][p] = 0.7 * st["scores"][p] + 0.3 * rets[p]       # recent hours count more
    weights = clamp_weights({p: math.exp(st["scores"][p] / 0.5) for p in st["grids"]})
    targets = {p: weights[p] * E for p in st["grids"]}

    # decide which coins to rebuild (new step or a big change in money)
    rebuild = {}
    for p, g in st["grids"].items():
        eq = g.equity(prices[p])
        step = live_step(st["hist"][p], now) or st["steps"][p]
        step_chg = abs(step - st["steps"][p]) / st["steps"][p] > REBUILD_DIFF
        money_chg = abs(targets[p] - eq) / max(eq, 1) > REBUILD_DIFF
        if step_chg or money_chg:
            rebuild[p] = step
            if step_chg:
                notes.append(f"{short(p)} الخطوة {st['steps'][p]*100:.2f}% ← {step*100:.2f}%")
            if money_chg:
                notes.append(f"{short(p)} المبلغ {eq:.0f}$ ← {targets[p]:.0f}$")
    for p in rebuild:                                  # close virtual positions into the bank
        g = st["grids"][p]
        before = len(g.fills)
        g._sell_all(prices[p], now, "rebalance")
        for f in g.fills[before:]:
            record_fill(p, f, g.cash)
        st["bank"] += g.cash
        g.cash = 0.0
    want = sum(targets[p] for p in rebuild)
    scale = min(1.0, st["bank"] / want) if want else 0
    for p, step in rebuild.items():
        alloc = targets[p] * scale
        st["bank"] -= alloc
        st["grids"][p] = new_grid(alloc, prices[p], step)
        st["steps"][p] = step

    hour_pnl = E - st["hour_E"]
    trades = sum(st["hour_trades"].values())
    st["hours"] += 1
    st["good_hours"] += 1 if hour_pnl > 0 else 0
    best = max(rets, key=rets.get)
    worst = min(rets, key=rets.get)
    lesson = (f"hour {st['hours']}: {trades} trades, {hour_pnl:+.2f}$ | best {short(best)} {rets[best]:+.2f}% "
              f"| worst {short(worst)} {rets[worst]:+.2f}% | changes: {', '.join(notes) or 'none'}")
    append_csv(LESSONS, ["time", "hour", "trades", "hour_pnl", "equity", "best", "worst", "changes",
                         "weights"],
               [datetime.now(RIYADH).isoformat(timespec="seconds"), st["hours"], trades,
                f"{hour_pnl:.2f}", f"{E:.2f}", f"{short(best)} {rets[best]:+.2f}%",
                f"{short(worst)} {rets[worst]:+.2f}%", "; ".join(notes),
                " ".join(f"{short(p)}:{weights[p]*100:.0f}%" for p in weights)])
    log(lesson)
    if TG_HOURLY:
        tg(f"🧠 بوت الشبكة - الساعة {st['hours']}\n"
           f"الصفقات: {trades} | النتيجة: {hour_pnl:+.2f}$\n"
           f"✅ الأفضل: {short(best)} {rets[best]:+.2f}%\n"
           f"❌ الأسوأ: {short(worst)} {rets[worst]:+.2f}%\n"
           f"🔧 التعديل: {'، '.join(notes) or 'ما فيه تعديل'}\n"
           f"💰 الرصيد: {E:.2f}$ ({(E / st['start_capital'] - 1) * 100:+.2f}%) | "
           f"ساعات رابحة {st['good_hours']} من {st['hours']}")
    st["hour_t"] = now
    st["hour_eq"] = {p: g.equity(prices[p]) for p, g in st["grids"].items()}
    st["hour_E"] = total_equity(st, prices)    # after rebalancing fees; bank money included
    st["hour_trades"] = {p: 0 for p in st["grids"]}


def status_report(st, prices):
    """Same layout as the hourly report, with real numbers since the current hour started."""
    E = total_equity(st, prices)
    rets = {p: (g.equity(prices[p]) / st["hour_eq"][p] - 1) * 100 if st["hour_eq"].get(p) else 0.0
            for p, g in st["grids"].items()}
    best, worst = max(rets, key=rets.get), min(rets, key=rets.get)
    return (f"🧠 بوت الشبكة - الساعة {st['hours'] + 1}\n"
            f"الصفقات: {sum(st['hour_trades'].values())} | النتيجة: {E - st['hour_E']:+.2f}$\n"
            f"✅ الأفضل: {short(best)} {rets[best]:+.2f}%\n"
            f"❌ الأسوأ: {short(worst)} {rets[worst]:+.2f}%\n"
            f"🔧 التعديل: ما فيه تعديل\n"
            f"💰 الرصيد: {E:.2f}$ ({(E / st['start_capital'] - 1) * 100:+.2f}%) | "
            f"ساعات رابحة {st['good_hours']} من {st['hours']}")


def record_fill(pair, fill, equity):
    ts, side, price, qty, pnl, reason = fill
    append_csv(FILLS, ["time", "pair", "side", "price", "qty", "pnl_usdt", "reason", "pair_equity"],
               [datetime.fromtimestamp(ts, RIYADH).isoformat(timespec="seconds"), pair, side,
                f"{price:.6f}", f"{qty:.8f}", f"{pnl:.4f}", reason, f"{equity:.2f}"])


def save(st):
    tmp = STATE + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(st, f)
    os.replace(tmp, STATE)


def main():
    os.makedirs(DATA, exist_ok=True)
    ex = ccxt.binance({"enableRateLimit": True})
    prices = {p: t["last"] for p, t in ex.fetch_tickers(PAIRS).items() if p in PAIRS}

    st = None
    if os.path.exists(STATE):
        try:
            st = pickle.load(open(STATE, "rb"))
            if set(st["grids"]) != set(PAIRS):
                raise ValueError("coin list changed")
            log(f"resumed: hour {st['hours']}, equity {total_equity(st, prices):.2f}")
            tg(status_report(st, prices))
        except Exception as e:
            log(f"starting fresh ({e})")
            st = None
    if st is None:
        st = fresh_state(prices, CAPITAL)
        log(f"started {CAPITAL:.0f} USDT on {len(PAIRS)} coins")
        tg(f"🟢 بوت الشبكة اشتغل: {CAPITAL:.0f}$ وهمي على {len(PAIRS)} عملات. "
           f"يتعلم من صفقاته ويعدل نفسه كل ساعة.")
    seen = {p: len(g.fills) for p, g in st["grids"].items()}

    while True:
        try:
            now = time.time()
            tick = ex.fetch_tickers(PAIRS)
            for p, g in st["grids"].items():
                price = tick[p]["last"]
                prices[p] = price
                st["hist"][p].append((now, price))
                g.on_price(price, now)
                for fill in g.fills[seen[p]:]:
                    record_fill(p, fill, g.equity(price))
                    _, side, fp, qty, pnl, reason = fill
                    if side == "SELL" and reason == "grid":
                        st["hour_trades"][p] += 1
                        st["day_trades"] += 1
                    if TG_TRADES or reason == "stop":
                        kind = "⚠️ وقف خسارة: بيع" if reason == "stop" else ("🟢 شراء" if side == "BUY" else "🔴 بيع")
                        tg(f"{kind} {short(p)} بسعر {fp:.4f}" + (f" | النتيجة {pnl:+.3f}$" if side == "SELL" else ""))
                seen[p] = len(g.fills)

            if now - st["hour_t"] >= LEARN_SECS:
                learn(st, prices, now)
                seen = {p: len(g.fills) for p, g in st["grids"].items()}

            E = total_equity(st, prices)
            if E < st["start_capital"] * 0.1:              # balance gone -> refill and keep learning
                st = fresh_state(prices, st["start_capital"], st["refills"] + 1)
                seen = {p: len(g.fills) for p, g in st["grids"].items()}
                tg(f"⚠️ بوت الشبكة خسر رصيده - شحنته {st['start_capital']:.0f}$ من جديد "
                   f"(الشحنة رقم {st['refills']})")

            today = datetime.now(RIYADH).date()
            if today != st["day"]:
                s = (f"📊 SalemGrid day {st['day']}: {st['day_trades']} trades, "
                     f"day {E - st['day_eq']:+.2f}$, equity {E:.2f}$ "
                     f"({(E / st['start_capital'] - 1) * 100:+.2f}%), "
                     f"profitable hours {st['good_hours']}/{st['hours']}, refills {st['refills']}")
                log(s)
                tg(f"📊 ملخص يوم {st['day']} - بوت الشبكة\n"
                   f"الصفقات: {st['day_trades']} | نتيجة اليوم: {E - st['day_eq']:+.2f}$\n"
                   f"💰 الرصيد: {E:.2f}$ ({(E / st['start_capital'] - 1) * 100:+.2f}%)\n"
                   f"ساعات رابحة {st['good_hours']} من {st['hours']} | مرات الشحن {st['refills']}")
                st.update(day=today, day_eq=E, day_trades=0)
            save(st)
        except Exception:
            log("error: " + traceback.format_exc().splitlines()[-1])
        time.sleep(5)


if __name__ == "__main__":
    main()
