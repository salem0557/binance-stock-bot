"""Daily review for SalemGrid.

Replays the last Riyadh day (and the last 7 / 30 days) with real 1-minute Binance prices
using the same engine as the bot, explains what went wrong, tests other settings, and
updates grid/params.json + grid/journal.md when a better setting is found.

Rules (to avoid chasing yesterday's noise):
- a candidate must average >= 20 completed trades/day over the last 7 days
- each slice must be >= 5.5 USDT (Binance minimum order)
- score = 7-day return + 30-day return; switch only if it beats the current score by >= 0.5
Usage: python daily_review.py [--dry]     (--dry = report only, don't write files)
"""
import itertools, json, os, sys, time
from datetime import datetime, timedelta, timezone

import ccxt

from grid_core import Grid

HERE = os.path.dirname(os.path.abspath(__file__))
PARAMS = os.path.join(HERE, "params.json")
JOURNAL = os.path.join(HERE, "journal.md")
RIYADH = timezone(timedelta(hours=3))
UNIVERSE = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT", "DOGE/USDT"]
PAIR_SETS = [["BTC/USDT", "ETH/USDT"], ["ETH/USDT", "SOL/USDT"],
             ["BTC/USDT", "ETH/USDT", "SOL/USDT"], ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"]]
STEPS = [0.004, 0.005, 0.006, 0.008, 0.010, 0.012]
HALVES = [3, 4, 6, 8, 10]
STOPS = [0.02, 0.04, 0.06, None]
MIN_TRADES_DAY = 20
MIN_SLICE = 5.5


def exchange():
    cfg = {"enableRateLimit": True}
    url = os.getenv("BINANCE_PUBLIC_URL")       # e.g. https://data-api.binance.vision/api/v3
    if url:
        cfg["urls"] = {"api": {"public": url}}
        cfg["options"] = {"fetchMarkets": {"types": ["spot"]}}
    return ccxt.binance(cfg)


def fetch_1m(ex, pair, since_ms, until_ms):
    rows, t = [], since_ms
    while t < until_ms:
        batch = ex.fetch_ohlcv(pair, "1m", since=t, limit=1000)
        if not batch:
            break
        rows += [r for r in batch if r[0] < until_ms]
        t = batch[-1][0] + 60_000
    return rows


def replay(params, candles, start_ms, end_ms):
    """Run the grid over [start, end). Returns stats dict."""
    pairs = params["pairs"]
    each = params["capital"] / len(pairs)
    st = {"equity": 0.0, "trades": 0, "stops": 0, "stop_loss": 0.0, "recentres": 0,
          "fees": 0.0, "idle_hours": 0.0}
    for p in pairs:
        rows = [r for r in candles[p] if start_ms <= r[0] < end_ms]
        g = Grid(each, rows[0][1], step=params["step"], half_levels=params["half_levels"],
                 stop_below=params["stop_below"], now=rows[0][0] / 1000)
        last_fill_t = rows[0][0]
        for ts, o, h, l, c, _ in rows:
            n0 = len(g.fills)
            for px in ((o, l, h, c) if c >= o else (o, h, l, c)):
                g.on_price(px, ts / 1000)
            if len(g.fills) > n0:
                gap = (ts - last_fill_t) / 3_600_000
                if gap > 2:                      # 2+ hours with no trade = price left the grid
                    st["idle_hours"] += gap
                last_fill_t = ts
        st["equity"] += g.equity(rows[-1][4])
        for f in g.fills:
            st["fees"] += f[2] * f[3] * g.fee
            if f[1] == "SELL" and f[5] == "grid":
                st["trades"] += 1
            if f[5] == "stop":
                st["stops"] += 1
                st["stop_loss"] += f[4]
        st["recentres"] += g.recentres
    days = (end_ms - start_ms) / 86_400_000
    st["ret"] = (st["equity"] / params["capital"] - 1) * 100
    st["trades_day"] = st["trades"] / days
    return st


def candidates(capital):
    for pairs, step, half, stop in itertools.product(PAIR_SETS, STEPS, HALVES, STOPS):
        if capital / len(pairs) / (2 * half) < MIN_SLICE:
            continue
        yield {"pairs": pairs, "capital": capital, "step": step, "half_levels": half, "stop_below": stop}


def fmt(p):
    stop = "none" if p["stop_below"] is None else f"{p['stop_below']*100:.0f}%"
    return (f"{'+'.join(x.split('/')[0] for x in p['pairs'])}, step {p['step']*100:.1f}%, "
            f"{2*p['half_levels']} levels, stop {stop}")


def main():
    dry = "--dry" in sys.argv
    params = json.load(open(PARAMS))
    now = datetime.now(RIYADH)
    day_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = day_end - timedelta(days=1)
    ms = lambda d: int(d.timestamp() * 1000)
    end, d1, d7, d30 = ms(day_end), ms(day_start), ms(day_end - timedelta(days=7)), ms(day_end - timedelta(days=30))

    ex = exchange()
    need = sorted(set(UNIVERSE) | set(params["pairs"]))
    candles = {p: fetch_1m(ex, p, d30, end) for p in need}

    cur = {k: replay(params, candles, s, end) for k, s in (("1d", d1), ("7d", d7), ("30d", d30))}
    y = cur["1d"]

    # what went wrong yesterday
    issues = []
    if y["trades"] < MIN_TRADES_DAY:
        issues.append(f"عدد الصفقات {y['trades']} أقل من 20 (السعر طلع من الشبكة {y['idle_hours']:.0f} ساعة)")
    if y["stops"]:
        issues.append(f"وقف الخسارة اشتغل {y['stops']} مرة وكلف {y['stop_loss']:+.2f}$")
    if y["fees"] > abs(y["equity"] - params["capital"]) and y["ret"] < 0:
        issues.append(f"الرسوم ({y['fees']:.2f}$) أكبر من الحركة، الخطوة صغيرة")
    if y["ret"] < 0 and not issues:
        issues.append("السعر نزل والشبكة شالت عملات بسعر أعلى")

    # search better settings
    score = lambda a, b: a["ret"] + b["ret"]
    cur_score = score(cur["7d"], cur["30d"])
    best, best_score, best_stats = None, cur_score, None
    for cand in candidates(params["capital"]):
        s7 = replay(cand, candles, d7, end)
        if s7["trades_day"] < MIN_TRADES_DAY:
            continue
        s30 = replay(cand, candles, d30, end)
        sc = score(s7, s30)
        if sc > best_score + 0.5:
            best, best_score, best_stats = cand, sc, (s7, s30)

    lines = [f"## {day_start.date()}",
             f"- الإعداد v{params['version']}: {fmt(params)}",
             f"- أمس: {y['ret']:+.2f}% | {y['trades']} صفقة | رسوم {y['fees']:.2f}$ | "
             f"إعادة تمركز {y['recentres']}",
             f"- آخر 7 أيام: {cur['7d']['ret']:+.2f}% ({cur['7d']['trades_day']:.0f} صفقة/يوم) | "
             f"آخر 30 يوم: {cur['30d']['ret']:+.2f}% ({cur['30d']['trades_day']:.0f} صفقة/يوم)",
             "- الأخطاء: " + ("؛ ".join(issues) if issues else "ما فيه أخطاء واضحة")]
    if best:
        s7, s30 = best_stats
        best["version"] = params["version"] + 1
        lines.append(f"- التعديل → v{best['version']}: {fmt(best)} "
                     f"(7 أيام {s7['ret']:+.2f}% بـ {s7['trades_day']:.0f} صفقة/يوم، 30 يوم {s30['ret']:+.2f}%)")
    else:
        lines.append("- القرار: ما فيه إعداد أفضل بشكل واضح، نكمل بنفس الإعداد")
    report = "\n".join(lines) + "\n"
    print(report)

    if not dry:
        with open(JOURNAL, "a") as f:
            f.write(report + "\n")
        if best:
            with open(PARAMS, "w") as f:
                json.dump({"version": best["version"], "pairs": best["pairs"], "capital": best["capital"],
                           "step": best["step"], "half_levels": best["half_levels"],
                           "stop_below": best["stop_below"]}, f, indent=2)
                f.write("\n")


if __name__ == "__main__":
    main()
