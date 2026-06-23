"""Smart-money directional bias — FREE alternative data (stock edition).

The crypto bot used Binance Futures' top-trader long/short ratio as a "where is
smart money leaning" gauge. Tokenized stocks (bStocks) have no such futures
feed, so the equivalent here is **relative strength (RS) versus the S&P 500**:

    RS = (stock return over N days) / (S&P 500 return over the same N days)

A stock that keeps OUTPERFORMING the broad market (RS > 1) is one institutions
are accumulating; one that persistently lags (RS < 1) is being distributed. Used
exactly like the old ratio: a buy is only confirmed when RS isn't badly below 1.

``long_short_bias(symbol)`` keeps the original name/semantics so the rest of the
bot is unchanged: it returns the RS ratio (e.g. 1.05 = outperforming, 0.92 =
lagging) or ``None`` when it can't be computed (the caller then stays neutral).

Calls are cached per symbol, so this never adds load to the fast trading loop.
"""

from __future__ import annotations

import time

import market
from exchange import _public_get

_TTL = 900            # seconds to cache a symbol's RS (daily data moves slowly)
_LOOKBACK = 10        # trading days of relative performance to measure
_CACHE = {}           # symbol -> (fetched_at, ratio_or_None)


def _stock_daily_closes(symbol, limit=20):
    """Recent daily closes for a bStock from Binance's public API."""
    try:
        rows = _public_get(f"/api/v3/klines?symbol={symbol}&interval=1d"
                           f"&limit={limit}")
        return [float(k[4]) for k in rows]
    except Exception:
        return []


def long_short_bias(symbol, ttl=_TTL):
    """Relative strength of ``symbol`` vs the S&P 500 (cached), or None.

    >1 = the stock is OUTPERFORMING the market (smart money accumulating);
    <1 = lagging. None = not computable — the caller stays neutral."""
    now = time.time()
    hit = _CACHE.get(symbol)
    if hit and (now - hit[0]) < ttl:
        return hit[1]
    ratio = None
    try:
        stock = _stock_daily_closes(symbol)
        spx = market.sp500_daily_closes()
        lb = _LOOKBACK
        if len(stock) > lb and len(spx) > lb \
                and stock[-lb - 1] > 0 and spx[-lb - 1] > 0:
            stock_ret = stock[-1] / stock[-lb - 1]
            spx_ret = spx[-1] / spx[-lb - 1]
            if spx_ret > 0:
                ratio = round(stock_ret / spx_ret, 3)
    except Exception:
        ratio = None
    _CACHE[symbol] = (now, ratio)
    return ratio
