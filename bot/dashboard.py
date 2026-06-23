"""Writes a JSON snapshot of the bot's state for the web dashboard.

The file is written to docs/stocks/data/bot.json so the page (and the built-in
web monitor) can show what the bot is doing — live positions, the self-tuned
parameters, recent trades, and performance — without exposing any API keys
(only public, non-sensitive status is written).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "docs" / "stocks" / "data" / "bot.json"


def write_snapshot(mode, symbols, active, params, positions, scores,
                   ml_acc, recent_trades, equity, realized_pnl,
                   last_optimize, prices, regime=None, learning=None,
                   account=None):
    snapshot = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "learning": learning or {},
        "regime": regime or {},
        "account": account or {},
        "universe": symbols,
        "active_symbols": active,
        "last_optimize": last_optimize,
        "equity_quote": round(equity, 2),
        "realized_pnl_quote": round(realized_pnl, 2),
        "positions": [
            {
                "symbol": s,
                "entry_price": round(p["entry_price"], 6),
                "qty": p["qty"],
                "price": round(prices.get(s, p["entry_price"]), 6),
                "pnl_pct": round(
                    (prices.get(s, p["entry_price"]) / p["entry_price"] - 1) * 100, 2)
                if p["entry_price"] else 0.0,
                "opened": p.get("opened"),
            }
            for s, p in positions.items()
        ],
        "strategy": [
            {
                "symbol": s,
                "params": params.get(s, {}),
                "backtest": scores.get(s, {}),
                "ml_accuracy": ml_acc.get(s),
                "active": s in active,
            }
            for s in symbols
        ],
        "recent_trades": recent_trades[-25:],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
