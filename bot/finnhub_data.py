"""Finnhub integration — FREE smart-trading signals for the underlying US stocks.

Optional. Activates only when ``FINNHUB_API_KEY`` is set (a free key from
https://finnhub.io — free tier is 60 calls/min, US stocks). Without a key every
function returns ``None`` and the bot behaves exactly as before.

bStocks are tokenized wrappers of real US shares, so we map the Binance symbol
to the underlying ticker (``NVDABUSDT`` → ``NVDA``) and ask Finnhub about the
real company:

  * analyst_bias(symbol)      — analyst recommendation trend as a 0..1 bullish
                                score: (strongBuy+buy) / all ratings. The stock
                                equivalent of a "smart money is long" signal.
  * earnings_within(symbol,d) — True if the company reports earnings within ``d``
                                days (huge gap risk → skip new entries).
  * news_score(symbol)        — keyword sentiment over recent company headlines.

Everything is cached and degrades to neutral (None) on any failure, so a blocked
network or a spent rate limit never blocks the bot.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import date, timedelta

API = "https://finnhub.io/api/v1"
_TTL = 1800            # cache responses for 30 min (these signals move slowly)
_CACHE = {}            # path -> (fetched_at, data)

# Reuse the news vocabulary already tuned for stocks.
try:
    from best_practices import POSITIVE, NEGATIVE
except Exception:      # pragma: no cover
    POSITIVE, NEGATIVE = [], []


def _key():
    """Read the key dynamically so a .env loaded after import still works."""
    return (os.environ.get("FINNHUB_API_KEY", "") or "").strip()


def enabled():
    return bool(_key())


def _get(path, ttl=_TTL):
    key = _key()
    if not key:
        return None
    now = time.time()
    hit = _CACHE.get(path)
    if hit and (now - hit[0]) < ttl:
        return hit[1]
    data = None
    try:
        sep = "&" if "?" in path else "?"
        url = f"{API}{path}{sep}token={key}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 stockbot/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except Exception:
        data = None
    _CACHE[path] = (now, data)
    return data


def ticker_of(symbol):
    """Binance bStock symbol -> underlying US ticker (NVDABUSDT -> NVDA)."""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    if base.endswith("B"):        # strip the bStock 'B' suffix (NVDAB -> NVDA)
        base = base[:-1]
    return base


def analyst_bias(symbol):
    """0..1 bullishness from the latest analyst recommendation trend, or None.

    (strongBuy + buy) / (all ratings). e.g. 0.7 = 70% of analysts say buy."""
    rows = _get(f"/stock/recommendation?symbol={ticker_of(symbol)}")
    if not rows or not isinstance(rows, list):
        return None
    r = rows[0]
    total = sum(int(r.get(k, 0) or 0) for k in
                ("strongBuy", "buy", "hold", "sell", "strongSell"))
    if total <= 0:
        return None
    bull = int(r.get("strongBuy", 0) or 0) + int(r.get("buy", 0) or 0)
    return round(bull / total, 3)


def earnings_within(symbol, days=2):
    """True if the company reports earnings within ``days`` (gap risk).

    False if not; None if unknown (no key / fetch failed) so the caller can
    treat 'unknown' as 'don't block'."""
    today = date.today()
    frm = today.isoformat()
    to = (today + timedelta(days=max(0, days))).isoformat()
    data = _get(f"/calendar/earnings?from={frm}&to={to}"
                f"&symbol={ticker_of(symbol)}", ttl=3600)
    if data is None or "earningsCalendar" not in data:
        return None
    return len(data.get("earningsCalendar") or []) > 0


def news_score(symbol, days=3):
    """Net keyword sentiment over recent company headlines, or None.

    Negative < 0 < positive, same scoring as best_practices."""
    today = date.today()
    frm = (today - timedelta(days=max(1, days))).isoformat()
    to = today.isoformat()
    rows = _get(f"/company-news?symbol={ticker_of(symbol)}"
                f"&from={frm}&to={to}", ttl=1800)
    if not rows or not isinstance(rows, list):
        return None
    score = 0
    for it in rows[:40]:
        text = f"{it.get('headline','')} {it.get('summary','')}".lower()
        score += sum(1 for w in POSITIVE if w in text)
        score -= sum(1 for w in NEGATIVE if w in text)
    return score
