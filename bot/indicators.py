"""Pure-Python technical indicators (no external deps).

Each function takes a list of float closing prices (oldest first) and returns
either a single value (the latest) or a list aligned to the input where the
first values that cannot be computed are ``None``.
"""

from __future__ import annotations


def sma(values, n):
    """Simple moving average of the last ``n`` values."""
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def sma_series(values, n):
    """SMA at every point (None until enough data)."""
    out = [None] * len(values)
    if n <= 0:
        return out
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= n:
            run -= values[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def ema_series(values, n):
    """Exponential moving average series."""
    out = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values, n=14):
    """Latest Wilder's RSI (0-100). Returns None if not enough data."""
    s = rsi_series(values, n)
    return s[-1] if s else None


def rsi_series(values, n=14):
    """RSI at every point using Wilder's smoothing."""
    out = [None] * len(values)
    if len(values) <= n:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain = gains / n
    avg_loss = losses / n
    out[n] = _rsi_from(avg_gain, avg_loss)
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        gain = max(d, 0.0)
        loss = max(-d, 0.0)
        avg_gain = (avg_gain * (n - 1) + gain) / n
        avg_loss = (avg_loss * (n - 1) + loss) / n
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr_pct(highs, lows, closes, n=14):
    """Average True Range as a percentage of price (volatility gauge)."""
    if len(closes) <= n:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    atr = sum(trs[-n:]) / n
    return (atr / closes[-1]) * 100 if closes[-1] else None


def pct_return(values, n):
    """Percentage return over the last ``n`` bars."""
    if len(values) <= n or values[-1 - n] == 0:
        return None
    return (values[-1] / values[-1 - n] - 1) * 100
