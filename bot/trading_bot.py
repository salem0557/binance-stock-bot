#!/usr/bin/env python3
"""Short-term INVESTING bot for Binance bStocks (tokenized US equities).

Methodology (NOT a tick scalper)
--------------------------------
A ~3-month position investor. Every rebalance it scores each stock by a
composite **investment** score — momentum + trend, tilted toward quality
(analyst conviction, earnings growth) and rewarded for dividend yield — and
HOLDS the best ``TOP_N`` names for weeks/months. It rotates only when a holding
breaks its trend, hits a wide protective stop, or is clearly displaced by a
better candidate. It holds through earnings and across ex-dividend dates.

Because bStocks are only days old, the decision is made from the UNDERLYING
stock's long history (NVDABUSDT → NVDA, years of daily data via Yahoo/Stooq)
and its fundamentals (Finnhub); execution happens on the tokenized bStock,
which tracks the real share 1:1.  — investor.py

Entry gates: top-ranked + healthy uptrend + broad market bullish (S&P 500 >
MA200) + not a VIX panic + no strong negative news.
Exits: wide stop-loss, trailing stop, trend break (under the 200-day line),
or rotation out of the top picks.

Modes (BOT_MODE), safest first: dryrun (default) / testnet / live. Educational
tool, NOT financial advice. bStocks are tokenized securities and aren't
available to US persons / some regions — make sure you're eligible. Start in
dryrun and only risk what you can afford to lose.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone, date
from pathlib import Path

from exchange import Exchange, bstocks_universe
import investor
import best_practices
import smart_money
import derivatives
import finnhub_data
import market
import publish
import monitor
import dashboard

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(HERE)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
TRADES_CSV = DATA_DIR / "trades.csv"

# Default basket = the curated Binance bStocks (tokenized US equities).
DEFAULT_UNIVERSE = ("NVDABUSDT,TSLABUSDT,CRCLBUSDT,SNDKBUSDT,MUBUSDT,"
                    "AMDBUSDT,INTCBUSDT,MSTRBUSDT,EWYBUSDT")


# ----------------------------- configuration -----------------------------
def load_env():
    env_path = HERE / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.split("#", 1)[0].strip())


def cfg(name, default=None):
    return os.environ.get(name, default)


def _flag(name, default):
    return (cfg(name, default) or "").lower() in ("1", "true", "yes", "on")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def iso():
    return datetime.now(timezone.utc).isoformat()


def log(msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    monitor.add_log(line)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "positions": {},        # symbol -> {entry_price, qty, opened, peak}
        "params": {},           # symbol -> {score, trend_ok}
        "scores": {},           # symbol -> investment metrics
        "ml_acc": {},           # symbol -> analyst bias (reused dashboard slot)
        "active": [],
        "target": [],           # the current top picks we want to hold
        "last_optimize": None,
        "realized_pnl": 0.0,
        "equity": 0.0,
        "trades": [],
        "day": str(date.today()),
        "day_start_realized": 0.0,
        "halted": False,
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def record_trade(state, side, symbol, price, qty, mode, reason=""):
    new = not TRADES_CSV.exists()
    with TRADES_CSV.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "mode", "symbol", "side", "price", "qty",
                        "quote_value", "reason"])
        w.writerow([now(), mode, symbol, side, f"{price:.8f}", f"{qty:.8f}",
                    f"{price * qty:.4f}", reason])
    state["trades"].append({
        "time": iso(), "symbol": symbol, "side": side,
        "price": round(price, 6), "qty": qty,
        "quote": round(price * qty, 2), "reason": reason,
    })
    state["trades"] = state["trades"][-50:]


# ------------------------------- the bot ---------------------------------
class Bot:
    def __init__(self):
        self.mode = (cfg("BOT_MODE", "dryrun") or "dryrun").lower()
        self.fixed_universe = [s.strip().upper()
                               for s in cfg("SYMBOLS", DEFAULT_UNIVERSE).split(",")
                               if s.strip()]
        self.auto_universe = _flag("AUTO_UNIVERSE", "false")
        self.min_quote_volume = float(cfg("MIN_QUOTE_VOLUME", "0"))
        self.max_universe = int(cfg("MAX_UNIVERSE", "60"))
        self._last_universe_ts = 0.0
        self.universe = list(self.fixed_universe)

        # --- portfolio / sizing ---
        self.top_n = int(cfg("TOP_N", "3"))
        self.max_open = int(cfg("MAX_OPEN_POSITIONS", "3"))
        self.quote_per_trade = float(cfg("QUOTE_PER_TRADE", "50"))
        self.min_score = float(cfg("MIN_SCORE", "0") or 0)

        # --- exits (wide, position-investing style) ---
        self.stop_loss = float(cfg("STOP_LOSS_PCT", "18") or 0)
        self.trailing = float(cfg("TRAILING_STOP_PCT", "15") or 0)
        self.take_profit = float(cfg("TAKE_PROFIT_PCT", "0") or 0)   # 0 = let winners run
        self.trend_exit = _flag("TREND_EXIT", "true")                # exit under SMA200
        self.max_spread = float(cfg("MAX_SPREAD_PCT", "0.8") or 0)

        # --- entry gates ---
        self.news_gate = _flag("NEWS_GATE", "true")
        self.deriv_gate = _flag("DERIVATIVES_GATE", "true")          # VIX panic / session
        self.trend_filter = _flag("MARKET_TREND_FILTER", "true")     # S&P > MA200
        self.trend_ma = int(cfg("MARKET_TREND_MA", "200"))
        self.pause_trading = _flag("PAUSE_TRADING", "false")

        # --- cadence (slow — this is investing, not scalping) ---
        self.poll_seconds = int(cfg("POLL_SECONDS", "300"))
        self.realtime = _flag("REALTIME_LEARNING", "false")
        self.optimize_hours = float(cfg("OPTIMIZE_HOURS", "12"))
        self.learn_seconds = float(cfg("LEARN_SECONDS", "3600"))

        self.daily_loss_limit = float(cfg("DAILY_LOSS_LIMIT", "0") or 0)
        self._last_skip_log = {}
        self.start_equity = float(cfg("PAPER_EQUITY", "500"))

        # publishing / durable state backup
        self.publish_on = _flag("PUBLISH_DASHBOARD", "")
        self.gh_repo = cfg("GH_REPO", "salem0557/binance-stock-bot")
        self.gh_token = cfg("GITHUB_TOKEN")
        self.pub_branch = cfg("PUBLISH_BRANCH", "bot-live")
        self.pub_seconds = int(cfg("PUBLISH_SECONDS", "60"))
        if not STATE_FILE.exists() and self.publish_on and self.gh_token:
            if publish.restore_state(self.gh_repo, self.pub_branch,
                                     self.gh_token, STATE_FILE):
                log("♻️  restored state from GitHub backup")

        self.state = load_state()
        self.ex = Exchange(self.mode, cfg("BINANCE_API_KEY"),
                           cfg("BINANCE_API_SECRET"))
        self._last_opt_ts = 0.0
        self._last_pub_ts = 0.0
        self._last_bal_ts = 0.0
        self._last_regime_ts = 0.0
        self._last_trend_ts = 0.0
        self._lock = threading.Lock()
        self.account = None
        self.market_bull = True
        self.market_trend_reason = "—"
        self.regime = {"allow_buys": True, "risk_multiplier": 1.0, "reason": "—"}
        self.candidates = list(self.fixed_universe)
        self.refresh_universe()

        if not self.state.get("equity"):
            self.state["equity"] = self.start_equity

        if self.mode == "live":
            if (cfg("CONFIRM_LIVE", "") or "").upper() != "I_UNDERSTAND_THE_RISK":
                raise SystemExit(
                    "LIVE mode refused. To trade REAL money set "
                    "CONFIRM_LIVE=I_UNDERSTAND_THE_RISK in your .env.")

    # ----------------------- universe -----------------------
    def refresh_universe(self):
        if not self.auto_universe:
            self.universe = list(self.fixed_universe)
            return
        try:
            uni = bstocks_universe(self.min_quote_volume, self.max_universe,
                                   auto_discover=True)
            if uni:
                self.universe = uni
                self._last_universe_ts = time.time()
                log(f"🌐 Auto-universe: tracking {len(uni)} bStocks")
        except Exception as e:
            log(f"universe build error: {e}")
            if not self.universe:
                self.universe = list(self.fixed_universe)

    # ----------------------- rebalance (the "learning" step) -----------------
    def rebalance(self):
        """Re-score every stock from its underlying's long history + fundamentals
        and pick the top names to hold. Network/CPU is done lock-free; only the
        short final apply takes the lock."""
        log("🧮 Rebalance — scoring underlying stocks (3-month view)")
        if self.auto_universe and (time.time() - self._last_universe_ts) >= 24 * 3600:
            self.refresh_universe()
        new_scores, evals = {}, {}
        for symbol in self.universe:
            try:
                ev = investor.evaluate(symbol)
            except Exception as e:
                log(f"   {symbol}: data error {e}")
                continue
            if ev is None:
                log(f"   {symbol}: skipped (not enough underlying history)")
                continue
            evals[symbol] = ev
            new_scores[symbol] = {
                "score": ev["score"], "return_pct": ev["ret_3m"],
                "ret_6m": ev["ret_6m"], "rs": ev["rs"],
                "trend_ok": ev["trend_ok"], "dividend_yield": ev["dividend_yield"],
                "eps_growth": ev["eps_growth"]}
            log(f"   {symbol} ({finnhub_data.ticker_of(symbol)}): "
                f"score={ev['score']} 3m={ev['ret_3m']}% rs={ev['rs']}% "
                f"trend={'↑' if ev['trend_ok'] else '↓'} "
                f"div={ev['dividend_yield']}% analyst={ev['analyst']}")

        eligible = [(s, ev) for s, ev in evals.items()
                    if ev["trend_ok"] and ev["score"] > self.min_score]
        eligible.sort(key=lambda x: x[1]["score"], reverse=True)
        target = [s for s, ev in eligible][: self.top_n]
        analyst_map = {s: evals[s]["analyst"] for s in evals}

        with self._lock:
            self.state["scores"].update(new_scores)
            self.state["params"] = {
                s: {"score": new_scores[s]["score"],
                    "trend_ok": new_scores[s]["trend_ok"]} for s in new_scores}
            self.state["ml_acc"] = analyst_map
            active = list(target)
            for held in self.state["positions"]:     # keep held for managed exit
                if held not in active:
                    active.append(held)
            self.state["active"] = active
            self.state["target"] = target
            self.state["last_optimize"] = iso()
            self.candidates = list(self.universe)
            save_state(self.state)
        log(f"✅ Rebalance done. Top picks: {target or '(none qualify)'}")

    # ------------------------------ trading ------------------------------
    def open_position(self, symbol, price):
        if len(self.state["positions"]) >= self.max_open:
            return
        if self.max_spread > 0:
            sp = self.ex.spread_pct(symbol)
            if sp is not None and sp > self.max_spread:
                self._skip_log(symbol, f"spread {sp:.2f}% > {self.max_spread}% "
                               "(too thin to enter)")
                return
        quote = self.quote_per_trade * self.regime.get("risk_multiplier", 1.0)
        fill, qty = self.ex.buy(symbol, quote, price)
        with self._lock:
            self.state["positions"][symbol] = {
                "entry_price": fill, "qty": qty, "opened": iso(), "peak": fill}
            if self.mode == "dryrun":
                self.state["equity"] -= fill * qty
            record_trade(self.state, "BUY", symbol, fill, qty, self.mode, "invest")
        log(f"🟢 BUY {symbol} {qty:.6f} @ {fill:.4f}")

    def close_position(self, symbol, price, reason):
        pos = self.state["positions"].get(symbol)
        if not pos:
            return
        try:
            fill, qty = self.ex.sell(symbol, pos["qty"], price)
        except RuntimeError as e:
            log(f"⚠️  {symbol}: {e} — dropping position from tracking")
            with self._lock:
                self.state["positions"].pop(symbol, None)
            return
        with self._lock:
            pnl = (fill - pos["entry_price"]) * qty
            self.state["realized_pnl"] += pnl
            if self.mode == "dryrun":
                self.state["equity"] += fill * qty
            record_trade(self.state, "SELL", symbol, fill, qty, self.mode, reason)
            self.state["positions"].pop(symbol, None)
        pct = (fill / pos["entry_price"] - 1) * 100 if pos["entry_price"] else 0
        log(f"🔴 SELL {symbol} {qty:.6f} @ {fill:.4f} ({reason}) "
            f"P/L {pnl:+.2f} ({pct:+.2f}%)")

    def manage_symbol(self, symbol, prices):
        price = self.ex.last_price(symbol)
        prices[symbol] = price
        pos = self.state["positions"].get(symbol)
        target = set(self.state.get("target", []))
        sc = self.state["scores"].get(symbol, {})

        if pos:
            entry = pos["entry_price"]
            pos["peak"] = max(pos.get("peak", entry), price)
            change = (price / entry - 1) * 100 if entry else 0
            reason = None
            if self.stop_loss and change <= -self.stop_loss:
                reason = f"stop-loss {change:.1f}%"
            elif self.trailing and pos["peak"] > entry and \
                    price <= pos["peak"] * (1 - self.trailing / 100):
                drop = (price / pos["peak"] - 1) * 100
                reason = f"trailing stop ({drop:.1f}% from peak, P/L {change:+.1f}%)"
            elif self.take_profit and change >= self.take_profit:
                reason = f"take-profit {change:.1f}%"
            elif self.trend_exit and sc.get("trend_ok") is False:
                reason = f"trend break — under 200-day line (P/L {change:+.1f}%)"
            elif sc and symbol not in target:
                reason = f"rotation — dropped from top picks (P/L {change:+.1f}%)"
            if reason:
                self.close_position(symbol, price, reason)
        elif self.pause_trading:
            if symbol in target:
                self._skip_log(symbol, "trading paused (PAUSE_TRADING=true)")
        else:
            if symbol not in target:
                return
            if len(self.state["positions"]) >= self.max_open:
                return
            if self.trend_filter and not self.market_bull:
                self._skip_log(symbol, f"market downtrend ({self.market_trend_reason})")
                return
            if self.news_gate and not self.regime.get("allow_buys", True):
                self._skip_log(symbol, f"best-practices: {self.regime.get('reason')}")
                return
            snap = derivatives.snapshot(symbol)
            if self.deriv_gate and not snap["confirm_long"]:
                self._skip_log(symbol, f"market-risk veto — {snap['reason']}")
                return
            self.open_position(symbol, price)

    def _skip_log(self, symbol, msg):
        now_ts = time.time()
        if now_ts - self._last_skip_log.get(symbol, 0) >= 300:
            log(f"⏸️  {symbol} entry skipped — {msg}")
            self._last_skip_log[symbol] = now_ts

    # --------------------------- risk / kill ---------------------------
    def check_daily_limit(self):
        today = str(date.today())
        hit = False
        with self._lock:
            if self.state.get("day") != today:
                self.state["day"] = today
                self.state["day_start_realized"] = self.state["realized_pnl"]
                self.state["halted"] = False
            if self.daily_loss_limit > 0:
                day_pnl = self.state["realized_pnl"] - self.state["day_start_realized"]
                if day_pnl <= -abs(self.daily_loss_limit) and not self.state["halted"]:
                    self.state["halted"] = True
                    hit = True
        if hit:
            log("🛑 Daily loss limit hit. Pausing new entries until tomorrow.")

    # ------------------------- fast loop -------------------------
    def _handle_manual_sells(self):
        for symbol in monitor.drain_sell_requests():
            if symbol not in self.state["positions"]:
                log(f"↩️  manual sell ignored — no open position for {symbol}")
                continue
            try:
                price = self.ex.last_price(symbol)
                log(f"🖐️  manual sell requested for {symbol} @ {price:.6f}")
                self.close_position(symbol, price, "بيع يدوي (manual)")
            except Exception as e:
                log(f"⚠️  manual sell failed for {symbol}: {e}")

    def manage_cycle(self):
        self._handle_manual_sells()
        prices = {}
        for symbol in list(self.state.get("active", [])):
            try:
                if self.state.get("halted"):
                    if symbol in self.state["positions"]:
                        price = self.ex.last_price(symbol)
                        prices[symbol] = price
                        self.close_position(symbol, price, "daily halt")
                    continue
                self.manage_symbol(symbol, prices)
            except Exception as e:
                log(f"   {symbol}: error {e}")

        with self._lock:
            save_state(self.state)
            try:
                dashboard.write_snapshot(
                    self.mode, self.candidates, self.state["target"],
                    self.state["params"], self.state["positions"],
                    self.state["scores"], self.state["ml_acc"],
                    self.state["trades"], self.state["equity"],
                    self.state["realized_pnl"], self.state["last_optimize"],
                    prices, regime=self.regime, account=self.account,
                    learning={"realtime": self.realtime,
                              "poll_seconds": self.poll_seconds,
                              "optimize_hours": self.optimize_hours})
            except Exception as e:
                log(f"dashboard write error: {e}")

    # ----------------------- background worker -----------------------
    def background_loop(self):
        while True:
            try:
                self.check_daily_limit()
                self._refresh_balance()
                self._refresh_regime()
                self._refresh_market_trend()
                now_ts = time.time()
                interval = self.learn_seconds if self.realtime \
                    else self.optimize_hours * 3600
                if not self.state.get("scores") or \
                        (now_ts - self._last_opt_ts) >= interval:
                    self.rebalance()
                    self._last_opt_ts = now_ts
                self._publish()
            except Exception as e:
                log(f"background error: {e}")
            time.sleep(5)

    def _refresh_balance(self):
        if (time.time() - self._last_bal_ts) < 60:
            return
        try:
            summ = self.ex.account_summary()
        except Exception as e:
            summ = None
            if self.mode in ("live", "testnet"):
                log(f"⚠️  balance read error: {e}")
        if summ:
            with self._lock:
                self.account = summ
                self.state["equity"] = summ["total_usdt"]
            log(f"💰 balance: {summ['total_usdt']} USDT total, "
                f"{summ['free_usdt']} USDT free")
        self._last_bal_ts = time.time()

    def _refresh_market_trend(self):
        if not self.trend_filter:
            self.market_bull = True
            return
        if (time.time() - self._last_trend_ts) < 300:
            return
        self._last_trend_ts = time.time()
        try:
            t = market.index_trend(self.trend_ma)
            if t["bull"] != self.market_bull:
                log(f"🧭 market trend → "
                    f"{'BULL (longs on)' if t['bull'] else 'BEAR (longs paused)'}: "
                    f"{t['reason']}")
            self.market_bull = t["bull"]
            self.market_trend_reason = t["reason"]
        except Exception as e:
            log(f"market-trend error: {e}")
            self.market_bull = True

    def _refresh_regime(self):
        if (time.time() - self._last_regime_ts) < 300:
            return
        try:
            regime = best_practices.get_regime()
            with self._lock:
                self.regime = regime
                self.state["regime"] = regime
        except Exception as e:
            log(f"regime error: {e}")
        self._last_regime_ts = time.time()

    def _publish(self):
        if not (self.publish_on and self.gh_token):
            return
        if (time.time() - self._last_pub_ts) < self.pub_seconds:
            return
        ok = publish.publish(self.gh_repo, self.pub_branch, self.gh_token)
        publish.backup_state(self.gh_repo, self.pub_branch, self.gh_token, STATE_FILE)
        self._last_pub_ts = time.time()
        if not ok:
            log("⚠️  dashboard publish failed (check GITHUB_TOKEN / GH_REPO)")

    def run(self):
        port = cfg("PORT")
        if port:
            try:
                monitor.start(port)
                log(f"📊 Web monitor on port {port}")
            except Exception as e:
                log(f"monitor start error: {e}")

        uni = (f"{len(self.universe)} bStocks (auto)" if self.auto_universe
               else str(self.universe))
        log(f"Investor bot started — mode={self.mode}, universe={uni}, "
            f"TOP_N={self.top_n}, {self.quote_per_trade} USDT/position, "
            f"hold≈3 months, rebalance every {self.optimize_hours}h, "
            f"poll={self.poll_seconds}s")
        if self.mode == "live":
            log("⚠️  LIVE MODE — investing REAL money. Ctrl+C to stop.")

        try:
            self.rebalance()
            self._last_opt_ts = time.time()
        except Exception as e:
            log(f"initial rebalance error: {e}")

        if "--once" in sys.argv:
            self.manage_cycle()
            return

        threading.Thread(target=self.background_loop, daemon=True).start()
        while True:
            try:
                self.manage_cycle()
            except Exception as e:
                log(f"cycle error: {e}")
            time.sleep(self.poll_seconds)


if __name__ == "__main__":
    load_env()
    Bot().run()
