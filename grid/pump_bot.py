"""SalemPump - paper (virtual) momentum hunter: catches coins at the start of an explosive move
and rides the wave with a trailing stop. Live Binance public prices, no API key, no real orders.

Every SCAN_SECS:
  1. one ticker call for all USDT coins; remember each coin's price for the last 30 minutes
  2. coins up >= 2.5% in 15 minutes become candidates -> read their 5m chart
  3. ENTER if: last 5m volume >= 4x the 2-hour average, price breaks the 2-hour high,
     not already up > 40% in 24h, and Bitcoin is not dumping (> -1.5% in 1h)
Exits (checked every 5 seconds):
  - hard stop -4% from entry
  - trailing stop: after +4% follow 4% under the peak; after +10% follow 6% under the peak
  - failed breakout: after 45 minutes still under +1% -> out
Each trade uses 10% of the balance (max 5 at once). Refills to the start balance if it runs out.
Railway variables (optional): PUMP_CAPITAL (2000), PUMP_TG_HOURLY (on), GRID=off disables it.
"""
import csv, os, pickle, time, traceback
from collections import deque
from datetime import datetime, timezone, timedelta

import ccxt
import requests

from chart import STABLE

DATA = os.getenv("DATA_DIR", "/data")
STATE = os.path.join(DATA, "pump_state.pkl")
TRADES = os.path.join(DATA, "pump_trades.csv")
CAPITAL = float(os.getenv("PUMP_CAPITAL", "2000"))
TG_TOKEN, TG_CHAT = os.getenv("TG_TOKEN", ""), os.getenv("TG_CHAT_ID", "")
TG_HOURLY = os.getenv("PUMP_TG_HOURLY", "on") == "on"
RIYADH = timezone(timedelta(hours=3))

SCAN_SECS, WATCH_SECS = 30, 5
MIN_VOL_24H = 5_000_000          # USDT traded in 24h
MOVE_15M = 0.025                 # +2.5% in 15 minutes -> candidate
VOL_SPIKE = 4.0                  # last 5m volume vs 2h average
MAX_24H = 0.40                   # skip if already +40% in 24h (too late)
BTC_DUMP = -0.015                # skip entries if BTC fell more than 1.5% in 1h
SLOT = 0.10                      # 10% of balance per trade
MAX_OPEN = 5
FEE, SLIP = 0.001, 0.002         # 0.1% fee each side, 0.2% slippage on fast moves
HARD_STOP = 0.04
TRAIL_START, TRAIL_1, TRAIL_BIG, TRAIL_2 = 0.04, 0.04, 0.10, 0.06
FAIL_MINS, FAIL_MIN_GAIN = 45, 0.01
COOLDOWN = 6 * 3600


def log(msg):
    print(f"[PUMP] {datetime.now(RIYADH):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": msg}, timeout=10)
        except Exception:
            pass


def short(p):
    return p.split("/")[0]


def fresh_state(capital, refills=0):
    now = time.time()
    return {"cash": capital, "start_capital": capital, "refills": refills, "open": {},
            "closed": [], "cooldown": {}, "seen": {}, "hour_t": now, "hour_eq": capital,
            "hours": 0, "good_hours": 0, "day": datetime.now(RIYADH).date(), "day_eq": capital}


def equity(st, prices):
    return st["cash"] + sum(t["qty"] * prices.get(p, t["entry"]) for p, t in st["open"].items())


def usdt_pairs(tickers):
    out = {}
    for sym, t in tickers.items():
        if not sym.endswith("/USDT") or ":" in sym:
            continue
        base = sym.split("/")[0]
        if base in STABLE or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        if (t.get("quoteVolume") or 0) >= MIN_VOL_24H and (t.get("last") or 0) > 0:
            out[sym] = t
    return out


def price_ago(hist, now, secs):
    for t, px in hist:              # oldest first
        if t >= now - secs:
            return px
    return None


def confirm(ex, sym, t):
    """Read the 5m chart: volume spike + breakout of the 2-hour high."""
    c = ex.fetch_ohlcv(sym, "5m", limit=30)
    if len(c) < 26:
        return None
    last, prev = c[-1], c[-25:-1]
    avg_vol = sum(x[5] for x in prev) / len(prev)
    hi2h = max(x[2] for x in prev)
    spike = last[5] / avg_vol if avg_vol else 0
    if spike >= VOL_SPIKE and t["last"] > hi2h:
        return {"spike": spike, "hi2h": hi2h}
    return None


def record(row):
    new = not os.path.exists(TRADES)
    with open(TRADES, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["opened", "closed", "pair", "entry", "exit", "peak_pct", "pnl_usdt", "pnl_pct",
                        "minutes", "reason"])
        w.writerow(row)


def close(st, p, px, now, reason):
    t = st["open"].pop(p)
    exit_px = px * (1 - SLIP)
    got = t["qty"] * exit_px * (1 - FEE)
    pnl = got - t["cost"]
    st["cash"] += got
    pct = pnl / t["cost"] * 100
    mins = (now - t["t"]) / 60
    st["closed"].append({"t": now, "pnl": pnl, "pct": pct})
    st["cooldown"][p] = now + COOLDOWN
    record([datetime.fromtimestamp(t["t"], RIYADH).isoformat(timespec="seconds"),
            datetime.fromtimestamp(now, RIYADH).isoformat(timespec="seconds"), p,
            f"{t['entry']:.8g}", f"{exit_px:.8g}", f"{(t['peak'] / t['entry'] - 1) * 100:.2f}",
            f"{pnl:.2f}", f"{pct:.2f}", f"{mins:.0f}", reason])
    why = {"stop": "وقف الخسارة -4%", "trail": "الوقف المتحرك (الموجة رجعت)",
           "failed": "الانفجار ما كمّل خلال 45 دقيقة"}[reason]
    icon = "✅" if pnl > 0 else "❌"
    msg = (f"{icon} خروج {short(p)} | {pct:+.2f}% ({pnl:+.2f}$)\n"
           f"دخول {t['entry']:.6g} ← خروج {exit_px:.6g} | أعلى نقطة {(t['peak'] / t['entry'] - 1) * 100:+.1f}%\n"
           f"المدة {mins:.0f} دقيقة | السبب: {why}")
    log(msg.replace("\n", " | "))
    tg(msg)


def manage(st, prices, now):
    for p in list(st["open"]):
        if p not in prices:
            continue
        t, px = st["open"][p], prices[p]
        t["peak"] = max(t["peak"], px)
        gain_peak = t["peak"] / t["entry"] - 1
        gain = px / t["entry"] - 1
        if gain <= -HARD_STOP:
            close(st, p, px, now, "stop")
        elif gain_peak >= TRAIL_START:
            trail = TRAIL_2 if gain_peak >= TRAIL_BIG else TRAIL_1
            if px <= t["peak"] * (1 - trail):
                close(st, p, px, now, "trail")
        elif now - t["t"] >= FAIL_MINS * 60 and gain < FAIL_MIN_GAIN:
            close(st, p, px, now, "failed")


def scan(ex, st, tickers, now):
    pairs = usdt_pairs(tickers)
    for sym, t in pairs.items():
        h = st["seen"].setdefault(sym, deque(maxlen=80))
        h.append((now, t["last"]))
    btc = st["seen"].get("BTC/USDT")
    btc_1h = (tickers["BTC/USDT"]["last"] / price_ago(btc, now, 3600) - 1) if btc and price_ago(btc, now, 3600) else 0
    if btc_1h < BTC_DUMP or len(st["open"]) >= MAX_OPEN:
        return
    cands = []
    for sym, t in pairs.items():
        if sym in st["open"] or st["cooldown"].get(sym, 0) > now:
            continue
        old = price_ago(st["seen"][sym], now, 900)
        if not old or now - st["seen"][sym][0][0] < 840:      # need ~15 min of memory
            continue
        move = t["last"] / old - 1
        if move >= MOVE_15M and (t.get("percentage") or 0) / 100 < MAX_24H:
            cands.append((move, sym, t))
    for move, sym, t in sorted(cands, reverse=True)[:5]:
        if len(st["open"]) >= MAX_OPEN:
            break
        try:
            ok = confirm(ex, sym, t)
        except Exception:
            continue
        if not ok:
            continue
        stake = min(st["cash"], equity(st, {}) * SLOT)
        if stake < 20:
            break
        entry = t["last"] * (1 + SLIP)
        qty = stake / entry * (1 - FEE)
        st["cash"] -= stake
        st["open"][sym] = {"t": now, "entry": entry, "qty": qty, "cost": stake, "peak": entry}
        msg = (f"🚀 دخول {short(sym)} بسعر {entry:.6g} | المبلغ {stake:.0f}$\n"
               f"طلعت {move * 100:+.1f}% في 15 دقيقة | الحجم {ok['spike']:.0f}× المعتاد | "
               f"كسرت قمة الساعتين | 24 ساعة {(t.get('percentage') or 0):+.1f}%")
        log(msg.replace("\n", " | "))
        tg(msg)


def hourly(st, prices, now):
    E = equity(st, prices)
    pnl = E - st["hour_eq"]
    st["hours"] += 1
    st["good_hours"] += 1 if pnl > 0 else 0
    hour_trades = [c for c in st["closed"] if c["t"] >= st["hour_t"]]
    allc = st["closed"]
    wins = sum(1 for c in allc if c["pnl"] > 0)
    opened = ", ".join(f"{short(p)} {(prices.get(p, t['entry']) / t['entry'] - 1) * 100:+.1f}%"
                       for p, t in st["open"].items()) or "ما فيه"
    line = (f"🧠 صياد الانفجارات - الساعة {st['hours']}\n"
            f"صفقات مقفولة: {len(hour_trades)} | النتيجة: {pnl:+.2f}$\n"
            f"📂 مفتوحة الحين: {opened}\n"
            f"🎯 نسبة الفوز: {wins} من {len(allc)}\n"
            f"💰 الرصيد: {E:.2f}$ ({(E / st['start_capital'] - 1) * 100:+.2f}%) | "
            f"ساعات رابحة {st['good_hours']} من {st['hours']}")
    log(line.replace("\n", " | "))
    if TG_HOURLY:
        tg(line)
    st["hour_t"], st["hour_eq"] = now, E


def save(st):
    tmp = STATE + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(st, f)
    os.replace(tmp, STATE)


def main():
    os.makedirs(DATA, exist_ok=True)
    ex = ccxt.binance({"enableRateLimit": True})
    st = None
    if os.path.exists(STATE):
        try:
            st = pickle.load(open(STATE, "rb"))
            log(f"resumed: {len(st['open'])} open, cash {st['cash']:.2f}")
        except Exception as e:
            log(f"starting fresh ({e})")
    if st is None:
        st = fresh_state(CAPITAL)
        log(f"started with {CAPITAL:.0f} USDT")
        tg(f"🟢 صياد الانفجارات اشتغل: {CAPITAL:.0f}$ وهمي. يراقب كل عملات Binance كل 30 ثانية "
           f"ويدخل أول ما تبدأ عملة تنفجر.")
    prices, last_scan = {}, 0.0
    while True:
        try:
            now = time.time()
            if now - last_scan >= SCAN_SECS:
                tickers = ex.fetch_tickers()
                prices = {s: t["last"] for s, t in tickers.items() if t.get("last")}
                manage(st, prices, now)
                scan(ex, st, tickers, now)
                last_scan = now
            elif st["open"]:
                tk = ex.fetch_tickers(list(st["open"]))
                prices.update({s: t["last"] for s, t in tk.items() if t.get("last")})
                manage(st, prices, now)

            if now - st["hour_t"] >= 3600:
                hourly(st, prices, now)
            E = equity(st, prices)
            if E < st["start_capital"] * 0.1 and not st["open"]:
                st = fresh_state(st["start_capital"], st["refills"] + 1)
                tg(f"⚠️ صياد الانفجارات خسر رصيده - شحنته {st['start_capital']:.0f}$ من جديد "
                   f"(الشحنة رقم {st['refills']})")
            today = datetime.now(RIYADH).date()
            if today != st["day"]:
                day = [c for c in st["closed"] if datetime.fromtimestamp(c["t"], RIYADH).date() == st["day"]]
                wins = sum(1 for c in day if c["pnl"] > 0)
                s = (f"📊 ملخص يوم {st['day']} - صياد الانفجارات\n"
                     f"الصفقات: {len(day)} | الرابحة: {wins} | نتيجة اليوم: {E - st['day_eq']:+.2f}$\n"
                     f"💰 الرصيد: {E:.2f}$ ({(E / st['start_capital'] - 1) * 100:+.2f}%) | "
                     f"مرات الشحن {st['refills']}")
                log(s.replace("\n", " | "))
                tg(s)
                st.update(day=today, day_eq=E)
                st["closed"] = st["closed"][-500:]
            save(st)
        except Exception:
            log("error: " + traceback.format_exc().splitlines()[-1])
        time.sleep(WATCH_SECS)


if __name__ == "__main__":
    main()
