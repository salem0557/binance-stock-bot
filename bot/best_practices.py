"""Live "best-practices" market-regime gauge (stock edition).

Beyond raw price signals, good practice is to read the *environment*: breaking
negative news (earnings misses, downgrades, probes, recalls) and the market's
fear gauge. The crypto bot used the Crypto Fear & Greed index + BTC dominance;
the stock equivalent here is the **VIX** (the options-implied "fear index").
Every cycle the bot consults this module and adapts:

  * strongly negative breaking news     -> pause NEW buys (only manage exits)
  * VIX in panic (very high)            -> pause buys (market in free-fall)
  * VIX elevated                        -> trade smaller (reduce position size)
  * VIX very low (complacency)          -> trade slightly smaller (froth risk)

Everything degrades gracefully: if the news file or the network is unavailable
the regime is simply "neutral" and the bot trades normally.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import market

HERE = Path(__file__).resolve().parent
NEWS_FILE = HERE.parent / "docs" / "stocks" / "data" / "news.json"

# Equity-news keyword sentiment (stock vocabulary, not crypto).
NEGATIVE = ["miss", "misses", "downgrade", "cut", "cuts", "guidance cut",
            "warning", "warns", "probe", "investigation", "sec", "lawsuit",
            "sue", "sued", "recall", "layoff", "layoffs", "bankrupt",
            "bankruptcy", "fraud", "selloff", "sell-off", "plunge", "plummet",
            "crash", "tumble", "slump", "halt", "halted", "delist", "default",
            "short seller", "weak demand", "fear", "slowdown"]
POSITIVE = ["beat", "beats", "upgrade", "upgrades", "raises guidance", "record",
            "record high", "all-time high", "surge", "soar", "soars", "rally",
            "buyback", "dividend", "partnership", "approval", "approved",
            "strong demand", "earnings beat", "raises", "outperform", "inflows",
            "guidance raise", "expansion"]


def _news_sentiment():
    """Net sentiment score from recent headlines (negative<0<positive)."""
    try:
        data = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None, 0
    items = data.get("news", [])[:30]
    score = 0
    for it in items:
        text = f"{it.get('title','')} {it.get('summary','')}".lower()
        score += sum(1 for w in POSITIVE if w in text)
        score -= sum(1 for w in NEGATIVE if w in text)
    return score, len(items)


def get_regime():
    """Return the current market regime and how it should affect trading.

    Keeps the same dict shape the dashboard expects; ``fear_greed`` carries the
    VIX value and ``fear_greed_label`` its Arabic classification."""
    sentiment, n_news = _news_sentiment()
    vix = market.vix_level()
    vix_lbl = market.vix_label(vix)

    allow_buys = True
    risk_multiplier = 1.0
    reasons = []

    if sentiment is not None and sentiment <= -4:
        allow_buys = False
        reasons.append(f"أخبار سلبية قوية (مؤشّر {sentiment})")
    elif sentiment is not None and sentiment >= 4:
        reasons.append(f"أخبار إيجابية (مؤشّر {sentiment})")

    if vix is not None:
        if vix >= 40:
            allow_buys = False
            reasons.append(f"VIX {vix:.0f} هلع — إيقاف الشراء")
        elif vix >= 28:
            risk_multiplier = min(risk_multiplier, 0.5)
            reasons.append(f"VIX {vix:.0f} مرتفع — تصغير الحجم")
        elif vix < 13:
            risk_multiplier = min(risk_multiplier, 0.8)
            reasons.append(f"VIX {vix:.0f} هدوء شديد — حذر من الرضا المفرط")

    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "news_sentiment": sentiment,
        "news_count": n_news,
        "fear_greed": round(vix, 1) if vix is not None else None,
        "fear_greed_label": vix_lbl,
        "vix": round(vix, 1) if vix is not None else None,
        "allow_buys": allow_buys,
        "risk_multiplier": risk_multiplier,
        "reason": " | ".join(reasons) or "وضع طبيعي",
    }
