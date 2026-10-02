"""Paper spot-grid engine (shared by the live bot and the backtest).

Rules:
- Grid of N levels around the start price (+-RANGE). Each level holds one slice of the capital.
- Price falls through a level  -> buy one slice (unless the crash guard is on).
- Price rises through a level  -> don't sell yet; follow the price and sell only after it
  pulls back TRAIL from its peak ("if it keeps rising, wait").
- Crash guard: if price fell more than CRASH in the last CRASH_WINDOW seconds, stop buying.
- If price goes above the whole grid and everything is sold, re-centre the grid higher.
"""
import time


class Grid:
    def __init__(self, capital, price, rng=0.15, n=20, fee=0.001, trail=0.005,
                 crash=0.05, crash_window=4 * 3600, now=None):
        self.fee, self.trail, self.rng, self.n = fee, trail, rng, n
        self.crash, self.crash_window = crash, crash_window
        self.cash = capital
        self.fills = []          # (ts, side, price, qty, pnl)
        self.history = []        # (ts, price) for crash guard
        self.realized = 0.0
        self._build(price, time.time() if now is None else now)

    # ---------- setup ----------
    def _build(self, price, now):
        lo, hi = price * (1 - self.rng), price * (1 + self.rng)
        step = (hi - lo) / self.n
        self.levels = [lo + i * step for i in range(self.n + 1)]
        self.slice = (self.cash + self.coin_value(price)) / self.n
        self.buys = set()        # level index with a pending buy
        self.bags = {}           # sell level index -> (qty, cost)
        self.peak = {}           # sell level index -> highest price since crossed
        for i, lv in enumerate(self.levels):
            if lv < price - step / 2:
                self.buys.add(i)
            elif lv > price + step / 2 and self.cash >= self.slice:  # pre-buy coins for levels above
                self._buy(i - 1, price, now, pre=True)

    def coin_value(self, price):
        return sum(q for q, _ in getattr(self, "bags", {}).values()) * price

    def equity(self, price):
        return self.cash + self.coin_value(price)

    # ---------- actions ----------
    def _buy(self, i, price, now, pre=False):
        amt = min(self.slice, self.cash)
        if amt < 5:              # Binance minimum order
            return
        qty = amt / price * (1 - self.fee)
        self.cash -= amt
        self.bags[i + 1] = (qty, amt)
        self.buys.discard(i)
        self.fills.append((now, "BUY", price, qty, 0.0))

    def _sell(self, i, price, now):
        qty, cost = self.bags.pop(i)
        got = qty * price * (1 - self.fee)
        self.cash += got
        pnl = got - cost
        self.realized += pnl
        self.peak.pop(i, None)
        self.buys.add(i - 1)
        self.fills.append((now, "SELL", price, qty, pnl))

    def crashing(self, price, now):
        old = [p for t, p in self.history if t >= now - self.crash_window]
        return bool(old) and price < max(old) * (1 - self.crash)

    # ---------- main ----------
    def on_price(self, prev, price, now):
        self.history.append((now, price))
        if len(self.history) > 5000:
            self.history = [h for h in self.history if h[0] >= now - self.crash_window]
        lo, hi = min(prev, price), max(prev, price)
        # buys: price falling through a buy level
        if price < prev and not self.crashing(price, now):
            for i in sorted(self.buys, reverse=True):
                if lo <= self.levels[i] <= hi:
                    self._buy(i, self.levels[i], now)
        # sells: arm when price reaches the level, sell after a pullback from the peak
        for i in list(self.bags):
            if i >= len(self.levels):
                continue
            lv = self.levels[i]
            if i in self.peak:
                self.peak[i] = max(self.peak[i], hi)
                stop = max(lv, self.peak[i] * (1 - self.trail))
                if price <= stop:
                    self._sell(i, stop if lo <= stop else price, now)
            elif hi >= lv:
                self.peak[i] = hi
        # everything sold and price above the grid -> re-centre higher
        if not self.bags and price > self.levels[-1]:
            self._build(price, now)
