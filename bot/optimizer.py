"""Self-optimizer: every cycle it grid-searches strategy parameters against
recent market data and keeps the best-performing combination per symbol.

This is the "يتعلّم ويحدّث نفسه" part #1 — the bot re-tunes itself from the
latest price action instead of using fixed, stale settings.
"""

from __future__ import annotations

import itertools

from backtest import run_backtest
from strategy import DEFAULT_PARAMS

from strategy import use_scalp

# Search grid. Kept modest so a full cycle finishes quickly for many symbols.
GRID = {
    "fast": [5, 7, 9, 12],
    "slow": [21, 25, 30, 50],
    "rsi_buy_max": [65, 70, 75],
    "stop_loss_pct": [3.0, 5.0, 8.0],
    "take_profit_pct": [8.0, 12.0, 20.0],
}

# Scalp grid: faster averages, tighter stops/targets, dip-buy levels.
SCALP_GRID = {
    "fast": [3, 5, 8],
    "slow": [13, 21, 34],
    "rsi_dip": [35, 40, 45],
    "stop_loss_pct": [1.0, 1.5, 2.5],
    "take_profit_pct": [1.5, 2.5, 4.0],
}


def _candidates():
    grid = SCALP_GRID if use_scalp() else GRID
    keys = list(grid.keys())
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, combo))
        if params["fast"] >= params["slow"]:
            continue
        yield params


# Walk-forward validation. The old optimizer tuned parameters on the SAME
# candles it then traded — classic over-fitting: it picked whatever fit recent
# noise, which stopped working immediately and bled money. Now we tune on an
# older "train" window and SELECT by performance on a newer, unseen "test"
# window, so only settings that actually GENERALISE are traded.
TRAIN_FRAC = 0.70        # tune on the oldest 70%, validate on the newest 30%
# A candidate must make at least this many in-sample trades to be evaluated.
# Set to 1 (not higher) on purpose: low-frequency trend-following legitimately
# closes only once or twice, so a higher floor would wrongly reject the most
# profitable "ride a trend" settings. Over-fit flukes are caught instead by the
# OUT-OF-SAMPLE selection below + the thin-sample score penalty.
MIN_TRAIN_TRADES = 1
MIN_SPLIT_BARS = 160     # below this there isn't enough data to split honestly

_BLANK = {"score": 0.0, "return_pct": 0.0, "trades": 0,
          "win_rate": 0.0, "max_drawdown_pct": 0.0}


def optimize_symbol(closes):
    """Walk-forward best parameters for one symbol's recent ``closes``.

    Returns (best_params, best_metrics) where best_metrics are measured
    OUT-OF-SAMPLE (on candles the parameters were not tuned on). If nothing
    generalises, returns defaults with score 0 so the symbol stays inactive
    (the ranking only trades symbols with a positive score) — i.e. when no
    setting has a real edge the bot correctly sits flat instead of forcing a
    bad trade.
    """
    n = len(closes)
    if n < MIN_SPLIT_BARS:
        return dict(DEFAULT_PARAMS), dict(_BLANK)

    split = int(n * TRAIN_FRAC)
    train, test = closes[:split], closes[split:]

    best_params = dict(DEFAULT_PARAMS)
    best_metrics = dict(_BLANK)
    best_oos = -1e9

    for cand in _candidates():
        params = dict(DEFAULT_PARAMS)
        params.update(cand)
        m_train = run_backtest(train, params)
        # Must trade enough AND be profitable on the data it was tuned on...
        if m_train["trades"] < MIN_TRAIN_TRADES or m_train["score"] <= 0:
            continue
        # ...then it's judged ONLY on the unseen test window.
        m_test = run_backtest(test, params)
        if m_test["score"] > best_oos:
            best_oos = m_test["score"]
            best_metrics = m_test
            best_params = params

    return best_params, best_metrics
