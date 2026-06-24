"""Short-term INVESTING strategy (~3-month horizon) — the new methodology.

This is NOT a tick-by-tick trader. It ranks each stock by a composite
investment score and holds the best names for weeks/months, rotating only when
something clearly better appears or a holding breaks down. It rides the two
factors with the strongest real-world evidence over a 1–6 month horizon —
**momentum** and **trend** — tilted toward **quality** (analyst conviction,
earnings growth) and rewarded a little for **dividend yield**.

Key idea: bStocks are only days old, but we make the decision from the
UNDERLYING stock's long history (NVDABUSDT → NVDA, years of daily data via
Yahoo/Stooq) and its fundamentals (Finnhub). We then execute on the tokenized
bStock, which tracks the real share 1:1.

``evaluate(symbol)`` returns a dict (or None when there isn't enough history):
  score, ret_3m, ret_6m, trend_ok, rs, analyst, dividend_yield, eps_growth.
Higher score = a better 3-month buy-and-hold candidate.
"""

from __future__ import annotations

import market
import finnhub_data
from indicators import sma_series

# Lookbacks in trading days.
M3 = 63           # ~3 months
M6 = 126          # ~6 months
MIN_HISTORY = 130  # need at least ~6 months of the underlying to rank it


def _ret(closes, n):
    if len(closes) <= n or closes[-1 - n] == 0:
        return None
    return closes[-1] / closes[-1 - n] - 1.0


def underlying_closes(symbol):
    """Long daily history of the real company behind a bStock symbol."""
    return market.stock_daily_closes(finnhub_data.ticker_of(symbol))


def evaluate(symbol):
    """Composite 3-month investment view, or None if not enough history."""
    closes = underlying_closes(symbol)
    if len(closes) < MIN_HISTORY:
        return None
    price = closes[-1]
    sma50 = sma_series(closes, 50)[-1]
    sma200 = sma_series(closes, 200)[-1] if len(closes) >= 200 else None

    r3 = _ret(closes, M3) or 0.0
    r6 = _ret(closes, M6) or 0.0
    momentum = 0.6 * r3 + 0.4 * r6          # blended momentum (3m-weighted)

    # Healthy uptrend: above the 200-day line and the 50-day above structure.
    trend_ok = (sma200 is None or price >= sma200) \
        and (sma50 is None or price >= sma50)

    # Relative strength vs the S&P 500 over 3 months.
    spx = market.sp500_daily_closes()
    rs = 0.0
    sp3 = _ret(spx, M3) if len(spx) > M3 else None
    if sp3 is not None:
        rs = r3 - sp3

    analyst = finnhub_data.analyst_bias(symbol)       # 0..1 or None
    a = analyst if analyst is not None else 0.5
    dividend = finnhub_data.dividend_yield(symbol) or 0.0   # %
    eps_g = finnhub_data.earnings_growth(symbol)            # % or None

    # Composite score (momentum-led, trend-gated, quality-tilted, yield-bonus).
    score = (100.0 * momentum
             + (20.0 if trend_ok else -40.0)
             + 100.0 * rs
             + (a - 0.5) * 30.0
             + min(dividend, 6.0) * 1.5
             + (max(-50.0, min(50.0, eps_g)) * 0.1 if eps_g is not None else 0.0))

    return {
        "score": round(score, 2),
        "ret_3m": round(r3 * 100, 2),
        "ret_6m": round(r6 * 100, 2),
        "trend_ok": trend_ok,
        "rs": round(rs * 100, 2),
        "analyst": analyst,
        "dividend_yield": round(dividend, 2),
        "eps_growth": round(eps_g, 1) if eps_g is not None else None,
        "sma200": sma200,
        "price": price,
    }
