"""Chart reader + scanner for SalemGrid (live Binance candles, no API key).

read_chart(): reads the last ~25 hours of 15-minute candles for one coin and labels it
  "up"   - strong uptrend   (EMA20 > EMA50, price above EMA50, +1.5% in 4h)
  "down" - strong downtrend (EMA20 < EMA50, price below EMA50, -1.5% in 4h) -> grid stops buying
  "side" - sideways / choppy (best for a grid)
  and a grid score = volatility x choppiness (high = price swings a lot without going anywhere).

scan(): ranks the most traded sideways USDT coins on Binance by that grid score.
"""
STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "AEUR", "EUR", "USDE", "XUSD", "USD1",
          "BFUSD", "RLUSD", "PYUSD", "UST", "USTC", "PAXG", "WBTC", "WBETH", "BNSOL"}


def ema(values, n):
    k, out = 2 / (n + 1), []
    for v in values:
        out.append(v if not out else out[-1] + k * (v - out[-1]))
    return out


def rsi(closes, n=14):
    gains = losses = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    return 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)


def analyse(candles):
    """candles: [[ts, o, h, l, c, v], ...] 15m, oldest first (needs >= 60)."""
    closes = [c[4] for c in candles]
    e20, e50 = ema(closes, 20)[-1], ema(closes, 50)[-1]
    last = closes[-1]
    chg4h = last / closes[-17] - 1                         # 16 x 15m = 4 hours
    window = closes[-49:]                                  # last 12 hours
    path = sum(abs(b - a) for a, b in zip(window, window[1:]))
    er = abs(window[-1] - window[0]) / path if path else 1.0   # 0 = pure chop, 1 = straight line
    vol = sum((c[2] - c[3]) / c[4] for c in candles[-48:]) / 48  # avg 15m range %
    if e20 < e50 and last < e50 and chg4h < -0.015:
        regime = "down"
    elif e20 > e50 and last > e50 and chg4h > 0.015:
        regime = "up"
    else:
        regime = "side"
    return {"regime": regime, "chg4h": chg4h, "er": er, "vol": vol, "rsi": rsi(closes),
            "score": vol * (1 - er) * 100}


def read_chart(ex, pair):
    return analyse(ex.fetch_ohlcv(pair, "15m", limit=100))


def scan(ex, top=40, min_volume=20_000_000):
    """Return [(pair, info)] for the most traded USDT coins, best grid score first."""
    tickers = ex.fetch_tickers()
    pool = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT") or ":" in sym:
            continue
        base = sym.split("/")[0]
        if base in STABLE or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        if (t.get("quoteVolume") or 0) >= min_volume and (t.get("last") or 0) > 0:
            pool.append((t["quoteVolume"], sym))
    pool = [s for _, s in sorted(pool, reverse=True)[:top]]
    out = []
    for sym in pool:
        try:
            info = read_chart(ex, sym)
            if info["regime"] == "side":            # pumps and dumps are bad for a grid
                out.append((sym, info))
        except Exception:
            continue
    return sorted(out, key=lambda x: x[1]["score"], reverse=True)
