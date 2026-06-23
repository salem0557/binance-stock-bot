"""Machine-learning confirmation model — the "يتعلّم" part #2.

Every self-update cycle the bot retrains a small classifier per symbol on the
latest candles. The model predicts the probability that price will be higher
``HORIZON`` bars from now. A buy is only allowed when both the technical signal
fires AND the model is sufficiently bullish, which filters out many bad entries.

Honest training: the data is split time-ordered into train/validation, and the
reported accuracy is measured on the held-out validation set (data the model
never saw) — not on the training data. A model with no real edge
(validation accuracy below ``MIN_EDGE``) is treated as neutral so it can't
wrongly approve or veto trades.

scikit-learn is optional: if it (or numpy) isn't installed, ``predict_up`` just
returns ``None`` and the bot trades on the technical signal alone.
"""

from __future__ import annotations

import os
from pathlib import Path

from indicators import sma_series, rsi_series, ema_series

HERE = Path(__file__).resolve().parent
MODELS_DIR = Path(os.environ.get("DATA_DIR", str(HERE))) / "models"
HORIZON = 4          # predict direction 4 bars ahead
UP_THRESHOLD = 0.0   # "up" means return > 0 over the horizon
MIN_SAMPLES = 120    # need at least this many feature rows to train
MIN_EDGE = 0.52      # validation accuracy must beat this to be used

try:  # optional heavy deps
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier
    HAVE_SKLEARN = True
except Exception:  # pragma: no cover - depends on environment
    HAVE_SKLEARN = False

_CACHE = {}  # symbol -> (fitted model, validation_accuracy)


def _volatility(closes, i, n=10):
    if i < n:
        return 0.0
    rets = [closes[j] / closes[j - 1] - 1.0 for j in range(i - n + 1, i + 1)
            if closes[j - 1]]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    return (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5


def _volume_feats(vols, i, n=20):
    """Volume surge + 1-bar change (zero if volume unavailable). Real moves come
    with volume; quiet 'breakouts' are usually traps — so the model learns it."""
    if not vols or i < 1 or i >= len(vols):
        return 0.0, 0.0
    window = [v for v in vols[max(0, i - n + 1):i + 1] if v is not None]
    avg = (sum(window) / len(window)) if window else 0.0
    surge = (vols[i] / avg - 1.0) if avg else 0.0
    chg = (vols[i] / vols[i - 1] - 1.0) if vols[i - 1] else 0.0
    # clip extremes so one print doesn't dominate the tree
    return max(-3.0, min(5.0, surge)), max(-3.0, min(5.0, chg))


def _range_feats(highs, lows, closes, i):
    """Bar range (volatility) and where the close sits in the bar (buying
    pressure: ~1 = closed near the high, ~0 = near the low)."""
    if not highs or not lows or i >= len(highs) or i >= len(lows):
        return 0.0, 0.5
    hi, lo, c = highs[i], lows[i], closes[i]
    if hi is None or lo is None or c == 0 or hi <= lo:
        return 0.0, 0.5
    rng = (hi - lo) / c
    pos = (c - lo) / (hi - lo)
    return rng, pos


def _features_at(closes, fast, slow, rsi_s, ema12, ema26, i,
                 highs=None, lows=None, vols=None):
    """Feature vector for bar ``i`` (returns None if not computable).

    Always emits the SAME length whether or not OHLCV extras are supplied
    (missing series → neutral defaults), so train/predict stay aligned."""
    if i < 60:
        return None
    if None in (fast[i], slow[i], rsi_s[i], ema12[i], ema26[i]):
        return None
    price = closes[i]
    if price == 0 or slow[i] == 0:
        return None
    vol_surge, vol_chg = _volume_feats(vols, i)
    bar_range, close_pos = _range_feats(highs, lows, closes, i)
    return [
        fast[i] / slow[i] - 1.0,                 # trend
        (price / closes[i - 1] - 1.0),           # 1-bar return
        (price / closes[i - 5] - 1.0),           # 5-bar return
        (price / closes[i - 10] - 1.0),          # 10-bar return
        (price / closes[i - 20] - 1.0),          # 20-bar return
        rsi_s[i] / 100.0,                        # momentum
        ema12[i] / ema26[i] - 1.0,               # macd-ish
        price / fast[i] - 1.0,                   # stretch from mean
        _volatility(closes, i),                  # recent volatility
        vol_surge,                               # volume vs its average
        vol_chg,                                 # 1-bar volume change
        bar_range,                               # bar high-low range
        close_pos,                               # close position in bar
    ]


def _build_xy(closes, highs=None, lows=None, vols=None):
    fast = sma_series(closes, 7)
    slow = sma_series(closes, 25)
    rsi_s = rsi_series(closes, 14)
    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    X, y = [], []
    for i in range(len(closes) - HORIZON):
        feats = _features_at(closes, fast, slow, rsi_s, ema12, ema26, i,
                             highs, lows, vols)
        if feats is None:
            continue
        future = closes[i + HORIZON] / closes[i] - 1.0
        X.append(feats)
        y.append(1 if future > UP_THRESHOLD else 0)
    return X, y


def train(symbol, closes, highs=None, lows=None, vols=None):
    """Train and cache a model. Returns out-of-sample accuracy or None."""
    if not HAVE_SKLEARN:
        return None
    X, y = _build_xy(closes, highs, lows, vols)
    if len(X) < MIN_SAMPLES or len(set(y)) < 2:
        return None
    try:
        # time-ordered split — train on the older 75%, validate on the newer 25%
        split = int(len(X) * 0.75)
        Xtr, ytr = np.array(X[:split]), np.array(y[:split])
        Xva, yva = np.array(X[split:]), np.array(y[split:])
        if len(Xva) < 20 or len(set(ytr)) < 2:
            return None
        model = GradientBoostingClassifier(
            n_estimators=80, max_depth=3, learning_rate=0.05, random_state=0)
        model.fit(Xtr, ytr)
        acc = round(float(model.score(Xva, yva)), 3)   # honest, out-of-sample
        _CACHE[symbol] = (model, acc)
        MODELS_DIR.mkdir(exist_ok=True)
        try:
            import joblib
            joblib.dump((model, acc), MODELS_DIR / f"{symbol}.pkl")
        except Exception:
            pass
        return acc
    except Exception:
        return None


def _load(symbol):
    cached = _CACHE.get(symbol)
    if cached is not None:
        return cached
    try:
        import joblib
        path = MODELS_DIR / f"{symbol}.pkl"
        if path.exists():
            obj = joblib.load(path)
            model, acc = obj if isinstance(obj, tuple) else (obj, 0.0)
            _CACHE[symbol] = (model, acc)
            return model, acc
    except Exception:
        pass
    return None


def predict_up(symbol, closes, highs=None, lows=None, vols=None):
    """Probability (0-1) that price rises over the horizon, or None.

    Returns None (neutral) unless the model has a real validation edge, so an
    over-fit or useless model never blocks or approves a trade."""
    if not HAVE_SKLEARN:
        return None
    loaded = _load(symbol)
    if loaded is None:
        return None
    model, acc = loaded
    if acc < MIN_EDGE:                 # no proven edge → stay out of the way
        return None
    fast = sma_series(closes, 7)
    slow = sma_series(closes, 25)
    rsi_s = rsi_series(closes, 14)
    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    feats = _features_at(closes, fast, slow, rsi_s, ema12, ema26,
                         len(closes) - 1, highs, lows, vols)
    if feats is None:
        return None
    try:
        return round(float(model.predict_proba(np.array([feats]))[0][1]), 3)
    except Exception:
        return None
