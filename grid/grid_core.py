"""Classic spot-grid engine, shared by the live paper bot and the daily review.

- Geometric grid: levels are `step` apart (e.g. 0.008 = 0.8%), `half_levels` below and above
  the start price. Each level holds one slice of the capital.
- Price falls to a level below  -> buy one slice; it will be sold one level higher.
- Price rises to a level above  -> sell the slice bought one level lower (profit = one step).
- Price breaks above the grid with everything sold -> re-centre the grid at the new price.
- Price falls `stop_below` under the grid -> sell everything and re-centre lower
  (None = hold the coins and wait for the price to come back).
"""
import time

MIN_ORDER = 5.0  # Binance minimum order value (USDT)


class Grid:
    def __init__(self, capital, price, step=0.008, half_levels=8, fee=0.001,
                 stop_below=0.06, now=None):
        self.step, self.half, self.fee, self.stop_below = step, half_levels, fee, stop_below
        self.cash = capital
        self.bags = {}       # level index -> coin qty waiting to be sold at that level
        self.fills = []      # (ts, side, price, qty, pnl, reason)
        self.realized = 0.0
        self.recentres = 0
        self.build(price, time.time() if now is None else now)

    def coins(self):
        return sum(self.bags.values())

    def equity(self, price):
        return self.cash + self.coins() * price

    def build(self, price, now):
        """(Re)build the grid around `price`; buy coins for the sell levels above."""
        n = 2 * self.half
        self.levels = [price * (1 + self.step) ** (i - self.half) for i in range(n + 1)]
        self.ptr = self.half                       # the empty level (closest to price)
        self.slice = self.equity(price) / n
        # (always called with no coins held: at start, after a sell-out or a stop)
        for j in range(self.ptr + 1, n + 1):
            amt = min(self.slice, self.cash)
            if amt < MIN_ORDER:
                break
            self.cash -= amt
            self.bags[j] = amt / price * (1 - self.fee)
            self.fills.append((now, "BUY", price, self.bags[j], 0.0, "setup"))

    def _sell_all(self, price, now, reason):
        for j in list(self.bags):
            q = self.bags.pop(j)
            got = q * price * (1 - self.fee)
            self.cash += got
            cost = self.slice
            self.realized += got - cost
            self.fills.append((now, "SELL", price, q, got - cost, reason))

    def on_price(self, price, now):
        n = 2 * self.half
        # buys: price at or below the next level down
        while self.ptr > 0 and price <= self.levels[self.ptr - 1]:
            lv = self.levels[self.ptr - 1]
            if self.cash < max(MIN_ORDER, self.slice * 0.999):
                break
            self.cash -= self.slice
            q = self.slice / lv * (1 - self.fee)
            self.bags[self.ptr] = q
            self.ptr -= 1
            self.fills.append((now, "BUY", lv, q, 0.0, "grid"))
        # sells: price at or above the next level up
        while self.ptr < n and price >= self.levels[self.ptr + 1] and (self.ptr + 1) in self.bags:
            lv = self.levels[self.ptr + 1]
            q = self.bags.pop(self.ptr + 1)
            got = q * lv * (1 - self.fee)
            pnl = got - self.slice
            self.cash += got
            self.realized += pnl
            self.ptr += 1
            self.fills.append((now, "SELL", lv, q, pnl, "grid"))
        # broke out above with nothing left to sell -> follow the price up
        if self.ptr == n and price > self.levels[-1] and not self.bags:
            self.recentres += 1
            self.build(price, now)
        # crashed below the grid -> cut and re-centre lower
        elif self.stop_below is not None and price < self.levels[0] * (1 - self.stop_below):
            self._sell_all(price, now, "stop")
            self.recentres += 1
            self.build(price, now)
