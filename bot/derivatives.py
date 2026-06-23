"""Market-session & risk gate — FREE alternative data (stock edition).

The crypto bot used Binance Futures funding / open-interest / taker-flow to veto
buys into euphoric, crowded-long froth. Tokenized stocks (bStocks) have no such
derivatives feed, so the equivalent risk gate here is built from the things that
actually make a tokenized-stock entry dangerous:

  * **Market session** — bStocks trade 24/7, but the *underlying* shares only
    price during US regular trading hours (09:30–16:00 ET). Off-session the
    token can be thin and gap on the open. With ``TRADE_RTH_ONLY=true`` the gate
    only confirms entries during regular hours.
  * **Volatility panic (VIX)** — when the VIX spikes above ``PANIC_VIX`` the
    whole market is in free-fall; a long-only dip-buyer should stand aside. This
    is the direct equivalent of the old "don't chase crowded froth" funding veto
    (just on the fear side instead of the greed side).

``snapshot(symbol)`` keeps the original interface (the ``symbol`` argument is
accepted but the signals are market-wide). It returns a dict with a combined,
explainable ``confirm_long`` flag plus a ``reason``. Everything is cached and
degrades to neutral (confirm_long=True) on any failure.
"""

from __future__ import annotations

import os
import time

import market

_TTL = 120                 # session/VIX are market-wide; cache once for 2m
_CACHE = {}                # "_global" -> (fetched_at, snapshot_dict)

# VIX at/above this = panic/free-fall; stand aside (long-only). ~40 is a crash.
PANIC_VIX = float(os.environ.get("PANIC_VIX", "40") or 40)


def _flag(name, default):
    return (os.environ.get(name, default) or "").lower() in (
        "1", "true", "yes", "on")


def snapshot(symbol=None, ttl=_TTL):
    """Combined market-session / risk view (cached, market-wide).

    Returns: confirm_long (bool), reason (str), score (-1..1 lean), vix, rth.
    """
    now = time.time()
    hit = _CACHE.get("_global")
    if hit and (now - hit[0]) < ttl:
        return hit[1]

    rth_only = _flag("TRADE_RTH_ONLY", "false")
    sess = market.session_state()
    vix = market.vix_level()

    confirm_long = True
    score = 0.0
    reasons = []

    if rth_only and not sess["rth"]:
        confirm_long = False
        reasons.append(f"خارج جلسة السوق الأمريكية ({sess['et']})")

    if vix is not None:
        if vix >= PANIC_VIX:
            confirm_long = False           # market in free-fall — don't catch it
            score -= 0.5
            reasons.append(f"VIX مرتفع جداً {vix:.1f} (هلع — تجنّب الشراء)")
        elif vix >= 28:
            score -= 0.2
            reasons.append(f"VIX مرتفع {vix:.1f} (تقلّب عالٍ)")
        elif vix < 13:
            score += 0.1
            reasons.append(f"VIX منخفض {vix:.1f} (سوق هادئة)")

    snap = {
        "confirm_long": confirm_long,
        "reason": " | ".join(reasons) or "—",
        "score": round(score, 3),
        "vix": vix,
        "rth": sess["rth"],
        "session": sess["et"],
    }
    _CACHE["_global"] = (now, snap)
    return snap
