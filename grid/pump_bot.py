"""SalemPump - momentum hunter: catches coins at the start of an explosive move and rides the
wave with a trailing stop. Runs on paper (virtual money) or LIVE on the owner's Binance spot account.

Scans every liquid USDT coin (24h volume >= 10M). Protections:
  - large coins (whitelist) get 25 USDT per trade, small coins 10 USDT, max 1 small coin open
  - skip coins whose bid/ask spread is wider than 0.3%
  - daily loss limit: after -5 USDT realized in a Riyadh day, no new entries until tomorrow
Every SCAN_SECS:
  1. one ticker call for all USDT coins; remember each coin's price for the last 30 minutes
  2. coins up >= 2.5% in 15 minutes become candidates -> read their 5m chart
  3. ENTER if: last 5m volume >= 4x the 2-hour average, price breaks the 2-hour high,
     the 15m move is under 6% and the 24h move under 18% (not late), and Bitcoin is not dumping
Exits (checked every 5 seconds):
  - hard stop -4% from entry
  - trailing stop after +4%: 2.5% under the peak (peak +4..7%), 4% (peak +7..10%), 6% (peak +10%+)
  - failed breakout: after 45 minutes still under +1% -> out
  - no wave after 2 hours (never reached +4%) -> out

Money rules:
  - the bot trades at most PUMP_CAPITAL (default 50 USDT), 2 trades of half each
  - realized profit above the capital is set aside and never traded again (in LIVE mode it simply
    stays in the account as USDT; the bot cannot withdraw - the key must not allow withdrawals)
  - LIVE: if the trading balance falls to 60% of the capital, new entries stop until the owner
    restarts it. Paper: refills to the capital if it runs out.

Railway variables:
  PUMP_LIVE=on + BINANCE_KEY + BINANCE_SECRET -> real orders (spot only)
  PUMP_CAPITAL (50), PUMP_TG_HOURLY (on), GRID=off disables the bot
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
CAPITAL = float(os.getenv("PUMP_CAPITAL", "50"))
KEY, SECRET = os.getenv("BINANCE_KEY", ""), os.getenv("BINANCE_SECRET", "")
WANT_LIVE = os.getenv("PUMP_LIVE", "off") == "on"
LIVE = WANT_LIVE and bool(KEY and SECRET)
TG_TOKEN, TG_CHAT = os.getenv("TG_TOKEN", ""), os.getenv("TG_CHAT_ID", "")
TG_HOURLY = os.getenv("PUMP_TG_HOURLY", "on") == "on"
RIYADH = timezone(timedelta(hours=3))

SCAN_SECS, WATCH_SECS = 30, 5
MIN_VOL_24H = 10_000_000         # USDT traded in 24h
# Large, established coins get the full stake; everything else counts as a small coin
LARGE = {"BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "AVAX", "LINK", "DOT", "TON",
         "LTC", "BCH", "XLM", "SUI", "NEAR", "APT", "UNI", "ICP", "ETC", "HBAR", "FIL", "ATOM",
         "ARB", "OP", "AAVE", "INJ", "TAO", "ENA"}
MOVE_15M = 0.025                 # +2.5% in 15 minutes -> candidate
VOL_SPIKE = 4.0                  # last 5m volume vs 2h average
MAX_24H = 0.18                   # skip if already +18% in 24h (too late) - from live data
MAX_MOVE_15M = 0.06              # skip if already +6% in 15m (chasing) - from live data
BTC_DUMP = -0.015                # skip entries if BTC fell more than 1.5% in 1h
MAX_OPEN = 3                     # up to 3 trades at once
MIN_STAKE = 10                   # Binance minimum order is ~5 USDT; keep a margin
STAKE_LARGE, STAKE_SMALL = 25, 10
MAX_SMALL_OPEN = 2
MAX_SPREAD = 0.003               # skip if bid/ask spread > 0.3%
DAY_LOSS_LIMIT = 5.0             # stop new entries for the day after -5 USDT realized
FEE, SLIP = 0.001, 0.002         # paper only: 0.1% fee each side, 0.2% slippage
HARD_STOP = 0.04
TRAIL_START = 0.04
# trailing distance by how high the trade has gone: lock gains on small waves, room for big ones
TRAILS = [(0.10, 0.06), (0.07, 0.04), (0.04, 0.025)]   # (peak gain >=, give back from the peak)
LOCK_FROM, LOCK_SHARE = 0.10, 0.80     # from +10% peak gain, never give back more than 20% of it
FAIL_MINS, FAIL_MIN_GAIN = 45, 0.01
MAX_HOLD_MINS = 120              # no wave (+4%) after 2 hours -> out
COOLDOWN = 6 * 3600
LIVE_FLOOR = 0.60                # LIVE: stop new entries at 60% of the capital


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


def tag(st):
    return "💰 حقيقي" if st.get("live") else "🧪 وهمي"


def fresh_state(capital, live, refills=0):
    now = time.time()
    return {"cash": capital, "start_capital": capital, "live": live, "refills": refills, "open": {},
            "withdrawn": 0.0, "paused": False, "closed": [], "cooldown": {}, "seen": {},
            "hour_t": now, "hour_eq": capital, "hours": 0, "good_hours": 0,
            "day": datetime.now(RIYADH).date(), "day_eq": capital}


def equity(st, prices):
    return st["cash"] + sum(t["qty"] * prices.get(p, t["entry"]) for p, t in st["open"].items())


def book(st):
    return st["cash"] + sum(t["cost"] for t in st["open"].values())


def sweep(st):
    """Set realized profit above the capital aside; it is never traded again."""
    extra = book(st) - st["start_capital"]
    if extra > 0.005:
        st["cash"] -= extra
        st["withdrawn"] = st.get("withdrawn", 0.0) + extra
        return extra
    return 0.0


# ---------------- order execution ----------------
def _fees(o):
    fs = o.get("fees") or ([o["fee"]] if o.get("fee") else [])
    return [f for f in fs if f and f.get("cost")]


def _settle(ex, o, sym):
    if o.get("status") != "closed" or not o.get("filled"):
        time.sleep(1)
        o = ex.fetch_order(o["id"], sym)
    return o


def buy(ex, st, sym, stake, ref_px):
    """Returns (entry_price, qty_held, usdt_spent)."""
    if not st["live"]:
        entry = ref_px * (1 + SLIP)
        return entry, stake / entry * (1 - FEE), stake
    free_usdt = ex.fetch_balance()["free"].get("USDT", 0) or 0
    if free_usdt < stake:
        raise RuntimeError(f"not enough USDT in the account ({free_usdt:.2f} < {stake:.2f})")
    o = _settle(ex, ex.create_market_buy_order_with_cost(sym, stake), sym)
    base = short(sym)
    qty = o["filled"] - sum(f["cost"] for f in _fees(o) if f.get("currency") == base)
    spent = o["cost"] + sum(f["cost"] for f in _fees(o) if f.get("currency") == "USDT")
    entry = o.get("average") or o["cost"] / o["filled"]
    return entry, qty, spent


def sell(ex, st, sym, qty, ref_px):
    """Returns (exit_price, usdt_received, note)."""
    if not st["live"]:
        exit_px = ref_px * (1 - SLIP)
        return exit_px, qty * exit_px * (1 - FEE), ""
    base = short(sym)
    have = ex.fetch_balance()["free"].get(base, 0) or 0
    amt = float(ex.amount_to_precision(sym, min(qty, have)))
    min_cost = ((ex.market(sym).get("limits") or {}).get("cost") or {}).get("min") or 5
    if amt * ref_px < min_cost:       # leftover too small for Binance to accept
        return ref_px, 0.0, f"الكمية صغيرة ({amt * ref_px:.2f}$) وما تنباع، بقت في حسابك"
    o = _settle(ex, ex.create_order(sym, "market", "sell", amt), sym)
    got = o["cost"] - sum(f["cost"] for f in _fees(o) if f.get("currency") == "USDT")
    return o.get("average") or ref_px, got, ""


# ---------------- strategy ----------------
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
                        "minutes", "reason", "mode"])
        w.writerow(row)


WHY = {"stop": "وقف الخسارة -4%", "trail": "الوقف المتحرك (الموجة رجعت)",
       "failed": "الانفجار ما كمّل خلال 45 دقيقة", "timeout": "ما جت موجة خلال ساعتين",
       "reset": "تغيير الإعدادات"}


def close(ex, st, p, px, now, reason):
    t = st["open"][p]
    try:
        exit_px, got, note = sell(ex, st, p, t["qty"], px)
    except Exception as e:                       # keep the position, retry on the next check
        if not t.get("sell_err"):
            t["sell_err"] = True
            tg(f"⚠️ {tag(st)} ما قدرت أبيع {short(p)}: {str(e)[:150]}\nبحاول مرة ثانية كل 5 ثواني.")
        log(f"sell {p} failed: {e}")
        return False
    st["open"].pop(p)
    pnl = got - t["cost"]
    st["cash"] += got
    pct = pnl / t["cost"] * 100
    mins = (now - t["t"]) / 60
    st["closed"].append({"t": now, "pnl": pnl, "pct": pct})
    st["cooldown"][p] = now + COOLDOWN
    record([datetime.fromtimestamp(t["t"], RIYADH).isoformat(timespec="seconds"),
            datetime.fromtimestamp(now, RIYADH).isoformat(timespec="seconds"), p,
            f"{t['entry']:.8g}", f"{exit_px:.8g}", f"{(t['peak'] / t['entry'] - 1) * 100:.2f}",
            f"{pnl:.2f}", f"{pct:.2f}", f"{mins:.0f}", reason, "live" if st["live"] else "paper"])
    took = sweep(st)
    where = "تركته في حسابك برا التداول" if st["live"] else "سحبته برا الصفقات"
    msg = (f"{'✅' if pnl > 0 else '❌'} {tag(st)} خروج {short(p)} | {pct:+.2f}% ({pnl:+.2f}$)\n"
           f"دخول {t['entry']:.6g} ← خروج {exit_px:.6g} | أعلى نقطة {(t['peak'] / t['entry'] - 1) * 100:+.1f}%\n"
           f"المدة {mins:.0f} دقيقة | السبب: {WHY[reason]}"
           + (f"\n⚠️ {note}" if note else "")
           + (f"\n💵 الربح {took:.2f}$ {where} (المجموع {st['withdrawn']:.2f}$)" if took else ""))
    log(msg.replace("\n", " | "))
    tg(msg)
    return True


def manage(ex, st, prices, now):
    for p in list(st["open"]):
        if p not in prices:
            continue
        t, px = st["open"][p], prices[p]
        t["peak"] = max(t["peak"], px)
        gain_peak = t["peak"] / t["entry"] - 1
        gain = px / t["entry"] - 1
        if gain <= -HARD_STOP:
            close(ex, st, p, px, now, "stop")
        elif gain_peak >= TRAIL_START:
            trail = next(tr for lvl, tr in TRAILS if gain_peak >= lvl)
            stop = t["peak"] * (1 - trail)
            if gain_peak >= LOCK_FROM:                 # big wave: keep at least 80% of the best gain
                stop = max(stop, t["entry"] * (1 + gain_peak * LOCK_SHARE))
            if px <= stop:
                close(ex, st, p, px, now, "trail")
        elif now - t["t"] >= FAIL_MINS * 60 and gain < FAIL_MIN_GAIN:
            close(ex, st, p, px, now, "failed")
        elif now - t["t"] >= MAX_HOLD_MINS * 60:
            close(ex, st, p, px, now, "timeout")


def scan(ex, st, tickers, now):
    pairs = usdt_pairs(tickers)
    for sym, t in pairs.items():
        h = st["seen"].setdefault(sym, deque(maxlen=80))
        h.append((now, t["last"]))
    if st.get("paused"):
        return
    today = datetime.now(RIYADH).date()
    day_pnl = sum(c["pnl"] for c in st["closed"] if datetime.fromtimestamp(c["t"], RIYADH).date() == today)
    if day_pnl <= -DAY_LOSS_LIMIT:
        if st.get("day_stop") != today:
            st["day_stop"] = today
            tg(f"🛑 {tag(st)} خسارة اليوم وصلت {day_pnl:.2f}$، وقفت الدخول لين بكرا. "
               f"الصفقات المفتوحة تكمل لين تقفل.")
        return
    btc = st["seen"].get("BTC/USDT")
    old_btc = price_ago(btc, now, 3600) if btc else None
    btc_1h = tickers["BTC/USDT"]["last"] / old_btc - 1 if old_btc else 0
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
        if MOVE_15M <= move < MAX_MOVE_15M and (t.get("percentage") or 0) / 100 < MAX_24H:
            cands.append((move, sym, t))
    for move, sym, t in sorted(cands, reverse=True)[:5]:
        if len(st["open"]) >= MAX_OPEN:
            break
        small = short(sym) not in LARGE
        if small and sum(1 for p in st["open"] if short(p) not in LARGE) >= MAX_SMALL_OPEN:
            continue
        try:
            ok = confirm(ex, sym, t)
            if ok:
                ob = ex.fetch_order_book(sym, limit=5)
                bid, ask = ob["bids"][0][0], ob["asks"][0][0]
                ok["spread"] = (ask - bid) / ((ask + bid) / 2)
                if ok["spread"] > MAX_SPREAD:
                    log(f"skip {sym}: spread {ok['spread'] * 100:.2f}%")
                    ok = None
        except Exception:
            continue
        if not ok:
            continue
        stake = min(st["cash"], STAKE_SMALL if small else STAKE_LARGE)
        if stake < MIN_STAKE:
            break
        try:
            entry, qty, spent = buy(ex, st, sym, stake, t["last"])
        except Exception as e:
            log(f"buy {sym} failed: {e}")
            tg(f"⚠️ {tag(st)} ما قدرت أشتري {short(sym)}: {str(e)[:150]}")
            continue
        st["cash"] -= spent
        st["open"][sym] = {"t": now, "entry": entry, "qty": qty, "cost": spent, "peak": entry}
        msg = (f"🚀 {tag(st)} دخول {short(sym)} بسعر {entry:.6g} | المبلغ {spent:.2f}$\n"
               f"طلعت {move * 100:+.1f}% في 15 دقيقة | الحجم {ok['spike']:.0f}× المعتاد | "
               f"كسرت قمة الساعتين | 24 ساعة {(t.get('percentage') or 0):+.1f}%\n"
               f"{'🔸 عملة صغيرة' if small else '🔹 عملة كبيرة'} | السبريد {ok['spread'] * 100:.2f}%")
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
    line = (f"🧠 صياد الانفجارات {tag(st)} - الساعة {st['hours']}\n"
            f"صفقات مقفولة: {len(hour_trades)} | النتيجة: {pnl:+.2f}$\n"
            f"📂 مفتوحة الحين: {opened}\n"
            f"🎯 نسبة الفوز: {wins} من {len(allc)}\n"
            f"💰 رصيد التداول: {E:.2f}$ من {st['start_capital']:.0f}$\n"
            f"💵 الربح المتروك برا التداول: {st.get('withdrawn', 0):.2f}$ | "
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


def make_exchange(live):
    cfg = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
    if live:
        cfg.update(apiKey=KEY, secret=SECRET)
    ex = ccxt.binance(cfg)
    ex.load_markets()
    return ex


def main():
    os.makedirs(DATA, exist_ok=True)
    if WANT_LIVE and not LIVE:
        tg("⚠️ صياد الانفجارات: PUMP_LIVE=on بس مفاتيح Binance ناقصة، فشغّلته وهمي.")
    ex = make_exchange(LIVE)
    st = None
    if os.path.exists(STATE):
        try:
            st = pickle.load(open(STATE, "rb"))
            st.setdefault("live", False)
            st.setdefault("paused", False)
            log(f"resumed ({'live' if st['live'] else 'paper'}): {len(st['open'])} open, cash {st['cash']:.2f}")
        except Exception as e:
            log(f"starting fresh ({e})")
            st = None

    if st is not None and (st["start_capital"] != CAPITAL or st["live"] != LIVE):
        # settings changed: close the old run's trades with the old mode, then start fresh
        old_ex = ex if st["live"] == LIVE else make_exchange(st["live"])
        if st["open"]:
            tk = old_ex.fetch_tickers(list(st["open"]))
            for p in list(st["open"]):
                close(old_ex, st, p, tk[p]["last"], time.time(), "reset")
        if st["open"]:
            log("could not close old trades; keeping the old run")
        else:
            old_eq = st["cash"] + st.get("withdrawn", 0.0)
            old_cap, old_tag = st["start_capital"], tag(st)
            st = fresh_state(CAPITAL, LIVE)
            log(f"new run: {CAPITAL:.0f} USDT {'LIVE' if LIVE else 'paper'} (old run ended at {old_eq:.2f})")
            tg(f"🔄 صياد الانفجارات: بداية جديدة {tag(st)} برصيد {CAPITAL:.0f}$\n"
               f"التجربة السابقة ({old_tag} {old_cap:.0f}$) خلصت عند {old_eq:.2f}$ ({old_eq - old_cap:+.2f}$)\n"
               f"أي ربح فوق {CAPITAL:.0f}$ ما يرجع للتداول"
               + (" ويبقى في حسابك تسحبه وقت ما تبي." if LIVE else "."))
    if st is None:
        st = fresh_state(CAPITAL, LIVE)
        log(f"started {CAPITAL:.0f} USDT {'LIVE' if LIVE else 'paper'}")
        tg(f"🟢 صياد الانفجارات اشتغل {tag(st)} برصيد {CAPITAL:.0f}$. يراقب كل عملات Binance "
           f"كل 30 ثانية ويدخل أول ما تبدأ عملة تنفجر.")

    if st["live"]:                                  # prove the key works and the money is there
        try:
            free = ex.fetch_balance()["free"].get("USDT", 0) or 0
            ok = free >= min(st["cash"], STAKE_LARGE)
            log(f"live balance check: {free:.2f} USDT free")
            tg(f"{'✅' if ok else '⚠️'} {tag(st)} اتصلت بحسابك في Binance. الرصيد المتاح: {free:.2f} USDT\n"
               + (f"البوت يتداول بـ {st['start_capital']:.0f}$ بس من هذا المبلغ." if ok
                  else f"الرصيد أقل من المطلوب، اشحن USDT في Spot Wallet عشان يقدر يدخل صفقات."))
        except Exception as e:
            log(f"live balance check failed: {e}")
            tg(f"❌ {tag(st)} ما قدرت أتصل بحسابك في Binance: {str(e)[:200]}\n"
               f"تأكد من المفتاح وإن فيه صلاحية Spot Trading.")
    prices, last_scan = {}, 0.0
    while True:
        try:
            now = time.time()
            if now - last_scan >= SCAN_SECS:
                tickers = ex.fetch_tickers()
                prices = {s: t["last"] for s, t in tickers.items() if t.get("last")}
                manage(ex, st, prices, now)
                scan(ex, st, tickers, now)
                last_scan = now
            elif st["open"]:
                tk = ex.fetch_tickers(list(st["open"]))
                prices.update({s: t["last"] for s, t in tk.items() if t.get("last")})
                manage(ex, st, prices, now)

            if now - st["hour_t"] >= 3600:
                hourly(st, prices, now)
            E = equity(st, prices)
            if st["live"]:
                if not st["paused"] and book(st) < st["start_capital"] * LIVE_FLOOR:
                    st["paused"] = True
                    tg(f"🛑 {tag(st)} وقفت الدخول في صفقات جديدة: رصيد التداول نزل إلى {book(st):.2f}$ "
                       f"من {st['start_capital']:.0f}$. الصفقات المفتوحة تكمل لين تقفل. كلم Claude إذا تبي تكمل.")
            elif E < st["start_capital"] * 0.1 and not st["open"]:
                st = fresh_state(st["start_capital"], False, st["refills"] + 1)
                tg(f"⚠️ صياد الانفجارات خسر رصيده - شحنته {st['start_capital']:.0f}$ من جديد "
                   f"(الشحنة رقم {st['refills']})")
            today = datetime.now(RIYADH).date()
            if today != st["day"]:
                day = [c for c in st["closed"] if datetime.fromtimestamp(c["t"], RIYADH).date() == st["day"]]
                wins = sum(1 for c in day if c["pnl"] > 0)
                s = (f"📊 ملخص يوم {st['day']} - صياد الانفجارات {tag(st)}\n"
                     f"الصفقات: {len(day)} | الرابحة: {wins} | نتيجة اليوم: {E - st['day_eq']:+.2f}$\n"
                     f"💰 رصيد التداول: {E:.2f}$ من {st['start_capital']:.0f}$ | "
                     f"💵 الربح المتروك: {st.get('withdrawn', 0):.2f}$")
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
