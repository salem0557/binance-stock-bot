"""US stock-market context — FREE data, no API key (the stock equivalents of the
crypto bot's BTC-trend / Fear-&-Greed / dominance signals).

Tokenized US stocks (Binance **bStocks**) trade 24/7, but their fair value is
set by the *underlying* shares, which only trade during the US session. This
module gives the bot the broad-market context it needs:

  * sp500_daily_closes() — S&P 500 (SPY ETF) daily closes from Stooq (free CSV).
  * vix_level()          — CBOE Volatility Index (^VIX) from Stooq — the stock
                           market's "fear gauge" (replaces crypto Fear & Greed).
  * index_trend(ma)      — is the S&P above its long moving average? (bull/bear)
  * session_state()      — is the US market in regular trading hours right now?

Everything is cached and degrades to "neutral / unknown" on any failure, so a
blocked network never blocks the bot. Stooq is a free, key-less data source
(https://stooq.com); if it is unreachable the regime simply stays neutral.
"""

from __future__ import annotations

import csv
import io
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta, time as dtime, date

try:  # Python 3.9+ standard library
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = None

# Data sources (free, no API key). Stooq is tried first; if it's blocked from
# the deploy region (some hosts can't reach it), Yahoo Finance is the fallback.
_STOOQ = "https://stooq.com/q/d/l/?s={sym}&i=d"
_YAHOO = ("https://query1.finance.yahoo.com/v8/finance/chart/"
          "{sym}?interval=1d&range={rng}")
_UA = {"User-Agent": "Mozilla/5.0 stockbot/1.0"}
_cache = {}   # key -> (fetched_at, value)

# NYSE/Nasdaq full-day market holidays (extend yearly). On these dates the US
# cash market is closed, so the underlying shares aren't priced.
_HOLIDAYS = {
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}


def _http(url, timeout=20):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _stooq_closes(sym):
    """Daily closes (oldest first) from Stooq, or [] on any failure."""
    try:
        text = _http(_STOOQ.format(sym=urllib.parse.quote(sym, safe="")))
    except Exception:
        return []
    out = []
    for row in csv.reader(io.StringIO(text)):
        if not row or row[0] == "Date":
            continue
        try:
            out.append(float(row[4]))   # Date,Open,High,Low,Close,Volume
        except (IndexError, ValueError):
            continue
    return out


def _yahoo_closes(sym, rng="2y"):
    """Daily closes (oldest first) from Yahoo Finance, or [] on any failure."""
    try:
        text = _http(_YAHOO.format(sym=urllib.parse.quote(sym, safe=""), rng=rng))
        res = json.loads(text)["chart"]["result"][0]
        closes = res["indicators"]["quote"][0]["close"]
        return [float(c) for c in closes if c is not None]
    except Exception:
        return []


def _series(name, stooq_sym, yahoo_sym, ttl=3600):
    """Daily close series, cached. Tries Stooq, then Yahoo; serves the last good
    series (stale-but-usable) if both fail this time."""
    key = ("series", name)
    hit = _cache.get(key)
    now = time.time()
    if hit and (now - hit[0]) < ttl:
        return hit[1]
    closes = _stooq_closes(stooq_sym) or _yahoo_closes(yahoo_sym)
    if closes:
        _cache[key] = (now, closes)
        return closes
    return hit[1] if hit else []   # serve stale on failure


def sp500_daily_closes():
    """S&P 500 daily closes (SPY on Stooq, falling back to Yahoo)."""
    return _series("sp500", "spy.us", "SPY")


def vix_level():
    """Latest CBOE Volatility Index value (^VIX), or None."""
    closes = _series("vix", "^vix", "^VIX", ttl=1800)
    return closes[-1] if closes else None


def stock_daily_closes(ticker, rng="2y"):
    """Daily closes (oldest first) for ANY underlying US stock/ETF ticker, via
    Stooq (``ticker.us``) then Yahoo. Gives YEARS of history even when the
    tokenized bStock itself is only days old — so momentum/trend can be computed
    from the real company. Cached 6h."""
    t = (ticker or "").strip().upper()
    if not t:
        return []
    return _series(f"stk:{t}", f"{t.lower()}.us", t, ttl=21600)


def vix_label(vix):
    """Human label for a VIX level (Arabic)."""
    if vix is None:
        return None
    if vix >= 40:
        return "هلع"
    if vix >= 28:
        return "خوف"
    if vix >= 18:
        return "حذر"
    if vix >= 13:
        return "هدوء"
    return "طمأنينة"


def index_trend(ma=200):
    """Is the broad market (S&P 500) above its long moving average?

    Returns {bull, price, ma, reason}. Defaults to bull=True (don't block) when
    there isn't enough data — the same fail-open behaviour as the crypto bot."""
    closes = sp500_daily_closes()
    if len(closes) < ma:
        return {"bull": True, "price": None, "ma": None,
                "reason": "بيانات S&P غير كافية — لا حجب"}
    m = sum(closes[-ma:]) / ma
    price = closes[-1]
    bull = price >= m
    return {"bull": bull, "price": price, "ma": m,
            "reason": f"S&P {price:.0f} {'≥' if bull else '<'} MA{ma} {m:.0f}"}


def _now_et():
    if _ET is not None:
        return datetime.now(_ET)
    # Fallback if zoneinfo/tzdata is unavailable: approximate US Eastern as
    # UTC-4 (EDT). Off by an hour in winter, but only used for the soft
    # market-hours gate, which fails open.
    return (datetime.now(timezone.utc) - timedelta(hours=4)).replace(tzinfo=None)


def session_state():
    """Is the US cash market in regular trading hours (09:30–16:00 ET) now?"""
    et = _now_et()
    weekday = et.weekday() < 5
    holiday = et.date() in _HOLIDAYS
    t = et.time()
    rth = weekday and not holiday and dtime(9, 30) <= t <= dtime(16, 0)
    return {"rth": rth, "weekday": weekday, "holiday": holiday,
            "et": et.strftime("%Y-%m-%d %H:%M ET")}


def is_rth():
    """True when the US market is in regular trading hours right now."""
    return session_state()["rth"]


def minutes_to_close():
    """Minutes left until the US session close (16:00 ET), or None if the
    market is already closed. Used to flatten positions before the bell."""
    et = _now_et()
    if et.weekday() >= 5 or et.date() in _HOLIDAYS:
        return None
    close = et.replace(hour=16, minute=0, second=0, microsecond=0)
    if et.time() < dtime(9, 30) or et.time() > dtime(16, 0):
        return None
    return max(0.0, (close - et).total_seconds() / 60.0)
