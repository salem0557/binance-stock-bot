#!/usr/bin/env python3
"""Self-optimizing multi-stock trading bot for Binance bStocks (tokenized US
equities on Spot).

What it does
------------
* Trades a basket of tokenized US stocks (NVIDIA, Tesla, AMD, …) on Binance's
  spot market vs USDT (bStocks: NVDABUSDT, TSLABUSDT, …).
* Every cycle it RE-LEARNS:
    1. grid-searches strategy parameters on recent candles (back-testing) and
       keeps the best per stock  — optimizer.py
    2. retrains a small ML classifier per stock that confirms entries
       — ml_model.py
  then ranks stocks by back-test score and trades only the strongest ``TOP_N``.
* Six entry-quality gates (all must pass): technical signal, ML confirmation,
  news gate, market-trend (S&P 500 > MA200), relative strength vs the S&P, and
  a market-session / VIX risk gate.
* Risk controls: per-trade size, max open positions, stop-loss, take-profit,
  trailing stop, ATR-adaptive stop, and a daily-loss kill-switch.
* Writes a public status snapshot for the web dashboard (no keys exposed).

Modes (BOT_MODE), safest first:
  dryrun  (default) – simulates everything on real prices. Zero risk.
  testnet           – real orders, fake money (Binance Spot Testnet).
  live              – real orders, REAL money. You must opt in explicitly.

This is an educational tool, NOT financial advice. Bots lose money too. bStocks
are tokenized securities and are not available to US persons / some regions —
make sure you're eligible. Start in dryrun, graduate to testnet, and only ever
risk what you can afford to lose.
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
from optimizer import optimize_symbol
from strategy import latest_signal, merge_params, DEFAULT_PARAMS
from backtest import run_backtest
import ml_model
import best_practices
import smart_money
import derivatives
import market
import publish
import monitor
import dashboard

HERE = Path(__file__).resolve().parent
# DATA_DIR lets a cloud host keep state on a persistent volume across redeploys.
DATA_DIR = Path(os.environ.get("DATA_DIR", str(HERE)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
TRADES_CSV = DATA_DIR / "trades.csv"

# Default basket = the curated Binance bStocks (tokenized US equities).
DEFAULT_UNIVERSE = ("NVDABUSDT,TSLABUSDT,CRCLBUSDT,SNDKBUSDT,MUBUSDT,"
                    "AMDBUSDT,INTCBUSDT,MSTRBUSDT,EWYBUSDT")

# How strongly a proven ML edge nudges the active-stock ranking. The back-test
# score stays the foundation; this only re-orders stocks of comparable score so
# the active slots favour ones where the ML buy-filter actually has an edge.
EDGE_WEIGHT = 20.0


# ----------------------------- configuration -----------------------------
def load_env():
    env_path = HERE / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            # strip inline comments and whitespace
            os.environ.setdefault(k.strip(), v.split("#", 1)[0].strip())


def cfg(name, default=None):
    return os.environ.get(name, default)


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
        "positions": {},        # symbol -> {entry_price, qty, opened}
        "params": {},           # symbol -> tuned params
        "scores": {},           # symbol -> backtest metrics
        "ml_acc": {},           # symbol -> ml accuracy
        "active": [],
        "last_optimize": None,
        "realized_pnl": 0.0,
        "equity": 0.0,
        "trades": [],           # recent trades for dashboard
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
        # AUTO_UNIVERSE: auto-detect newly listed bStocks (any USDT pair whose
        # base asset looks like a tokenized stock) instead of the fixed basket.
        self.auto_universe = (cfg("AUTO_UNIVERSE", "false") or "").lower() \
            in ("1", "true", "yes", "on")
        self.min_quote_volume = float(cfg("MIN_QUOTE_VOLUME", "0"))
        self.max_universe = int(cfg("MAX_UNIVERSE", "60"))
        self.scan_batch = int(cfg("SCAN_BATCH", "20"))
        self.shortlist_n = int(cfg("SHORTLIST", "12"))
        self._scan_cursor = 0
        self._last_universe_ts = 0.0
        self.universe = list(self.fixed_universe)
        self.interval = cfg("INTERVAL", "1h")
        self.quote_per_trade = float(cfg("QUOTE_PER_TRADE", "50"))
        self.poll_seconds = int(cfg("POLL_SECONDS", "30"))
        # Real-time learning: re-tune parameters EVERY cycle. Set to false to
        # fall back to the slower OPTIMIZE_HOURS schedule.
        self.realtime = (cfg("REALTIME_LEARNING", "true") or "").lower() \
            in ("1", "true", "yes", "on")
        self.optimize_hours = float(cfg("OPTIMIZE_HOURS", "2"))
        # ML training is heavier than the grid-search, so it has its own
        # (still frequent) cadence. 0 = retrain every cycle too.
        self.ml_retrain_min = float(cfg("ML_RETRAIN_MINUTES", "10"))
        # Heavy scan/optimize runs every LEARN_SECONDS (not every poll), so
        # position-exit checks stay fast and react to price within POLL_SECONDS.
        self.learn_seconds = float(cfg("LEARN_SECONDS", "60"))
        self.top_n = int(cfg("TOP_N", "4"))
        self.history = int(cfg("HISTORY", "500"))
        self.max_open = int(cfg("MAX_OPEN_POSITIONS", "4"))
        self.daily_loss_limit = float(cfg("DAILY_LOSS_LIMIT", "0") or 0)
        # Trailing stop: ride the rise, sell when price pulls back this % from
        # its peak. 0 = off (use the fixed take-profit instead).
        self.trailing_pct = float(cfg("TRAILING_STOP_PCT", "0") or 0)
        # Min ML "up" probability to allow a buy (lower = more trades, riskier).
        self.ml_threshold = float(cfg("ML_BUY_THRESHOLD", "0.55") or 0.55)
        # Fixed stop-loss / take-profit % (0 = let the optimizer tune them).
        self.force_sl = float(cfg("STOP_LOSS_PCT", "0") or 0)
        self.force_tp = float(cfg("TAKE_PROFIT_PCT", "0") or 0)
        # Max bid/ask spread % allowed to enter (escape-clean filter). 0 = off.
        self.max_spread = float(cfg("MAX_SPREAD_PCT", "0.5") or 0)
        # News gate: veto buys on strong negative news. Off = trade through it
        # (handy since a deployed news.json may be stale).
        self.news_gate = (cfg("NEWS_GATE", "true") or "").lower() \
            in ("1", "true", "yes", "on")
        # Relative-strength "smart-money" gate: only confirm a buy when the stock
        # isn't badly lagging the S&P 500 (RS >= SMART_MONEY_MIN). Off = ignore.
        self.smart_gate = (cfg("SMART_MONEY_GATE", "true") or "").lower() \
            in ("1", "true", "yes", "on")
        self.smart_min = float(cfg("SMART_MONEY_MIN", "0.97") or 0.97)
        # Pause switch: when on, the bot opens NO new positions but still
        # manages and exits open ones safely (stop-loss / trailing / take-profit
        # all keep working). Set PAUSE_TRADING=true to halt buying.
        self.pause_trading = (cfg("PAUSE_TRADING", "false") or "").lower() \
            in ("1", "true", "yes", "on")
        # Market-trend filter: don't open longs while the broad US market is in a
        # downtrend (S&P 500 below its long moving average). A long-only bot
        # bleeds buying dips that keep falling; this keeps it flat in bear
        # phases. Off = MARKET_TREND_FILTER=false.
        self.trend_filter = (cfg("MARKET_TREND_FILTER", "true") or "").lower() \
            in ("1", "true", "yes", "on")
        # MA length on the S&P 500 DAILY series (200 = the classic trend line).
        self.trend_ma = int(cfg("MARKET_TREND_MA", "200"))
        self.market_bull = True
        self.market_trend_reason = "—"
        self._last_trend_ts = 0.0
        # Stock-quality filter: skip auto-discovered names whose recent per-bar
        # volatility exceeds this %. 0 = off.
        self.max_volatility = float(cfg("MAX_VOLATILITY_PCT", "0") or 0)
        # Market-session / VIX risk gate (the funding-froth equivalent): veto a
        # buy when the market is in panic (high VIX) or, optionally, outside US
        # regular trading hours. Off = DERIVATIVES_GATE=false.
        self.deriv_gate = (cfg("DERIVATIVES_GATE", "true") or "").lower() \
            in ("1", "true", "yes", "on")
        # ATR volatility stop: stop distance = max(tuned %, ATR×mult). 0 = off
        # (use only the optimizer's fixed %). ~1.5–2.5 adapts stops to each name.
        self.atr_stop_mult = float(cfg("ATR_STOP_MULT", "0") or 0)
        self.atr_period = int(cfg("ATR_PERIOD", "14"))
        self._last_skip_log = {}
        self.start_equity = float(cfg("PAPER_EQUITY", "500"))

        # Publishing / durable state backup config (read early so we can
        # restore state from GitHub before loading it on a stateless host).
        self.publish_on = (cfg("PUBLISH_DASHBOARD", "") or "").lower() \
            in ("1", "true", "yes", "on")
        self.gh_repo = cfg("GH_REPO", "salem0557/binance-stock-bot")
        self.gh_token = cfg("GITHUB_TOKEN")
        self.pub_branch = cfg("PUBLISH_BRANCH", "bot-live")
        self.pub_seconds = int(cfg("PUBLISH_SECONDS", "60"))

        # On a stateless cloud host, restore state.json from GitHub if absent.
        if not STATE_FILE.exists() and self.publish_on and self.gh_token:
            if publish.restore_state(self.gh_repo, self.pub_branch,
                                     self.gh_token, STATE_FILE):
                log("♻️  restored state from GitHub backup")

        self.state = load_state()
        self.ex = Exchange(self.mode, cfg("BINANCE_API_KEY"),
                           cfg("BINANCE_API_SECRET"))
        self._last_opt_ts = 0.0
        self._last_ml_ts = 0.0
        self._last_pub_ts = 0.0
        self._last_bal_ts = 0.0
        self._last_regime_ts = 0.0
        self._lock = threading.Lock()   # guards shared state across threads
        self.account = None      # real Binance balance (live/testnet)
        self.regime = {"allow_buys": True, "risk_multiplier": 1.0,
                       "reason": "—"}
        self.state.setdefault("scan_scores", {})
        self.candidates = list(self.fixed_universe)
        self.refresh_universe()

        if not self.state.get("equity"):
            self.state["equity"] = self.start_equity

        if self.mode == "live":
            confirm = (cfg("CONFIRM_LIVE", "") or "").upper()
            if confirm != "I_UNDERSTAND_THE_RISK":
                raise SystemExit(
                    "LIVE mode refused. To trade REAL money set "
                    "CONFIRM_LIVE=I_UNDERSTAND_THE_RISK in your .env.")

    # ----------------------- universe / scanning -----------------------
    def refresh_universe(self):
        """Build the trading universe (auto = auto-detected bStocks)."""
        if not self.auto_universe:
            self.universe = list(self.fixed_universe)
            return
        try:
            uni = bstocks_universe(self.min_quote_volume, self.max_universe,
                                   auto_discover=True)
            if uni:
                self.universe = uni
                self._last_universe_ts = time.time()
                log(f"🌐 Auto-universe: tracking {len(uni)} bStocks pairs "
                    f"(by 24h volume)")
        except Exception as e:
            log(f"universe build error: {e}")
            if not self.universe:
                self.universe = list(self.fixed_universe)

    def _scan_pass(self):
        """Quick-score the next batch of the universe (rotating). Network/CPU is
        done lock-free; returns (updates, drops) to apply under the lock."""
        updates, drops = {}, []
        if not self.universe:
            return updates, drops
        n = len(self.universe)
        batch = [self.universe[(self._scan_cursor + i) % n]
                 for i in range(min(self.scan_batch, n))]
        self._scan_cursor = (self._scan_cursor + len(batch)) % n
        for symbol in batch:
            try:
                closes = self.ex.closes(symbol, self.interval,
                                        min(self.history, 200))
                if len(closes) >= 60:
                    # Quality gate: drop hyper-volatile names before they can
                    # become candidates (only when MAX_VOLATILITY_PCT > 0).
                    if self.max_volatility > 0 and \
                            self._recent_vol_pct(closes) > self.max_volatility:
                        drops.append(symbol)
                        continue
                    updates[symbol] = run_backtest(closes, DEFAULT_PARAMS)["score"]
            except Exception:
                drops.append(symbol)
        return updates, drops

    @staticmethod
    def _atr_pct(highs, lows, closes, period=14):
        """Average True Range over ``period`` bars, as a % of price. Captures a
        name's typical breathing room so stops can adapt to its volatility."""
        n = len(closes)
        if n < period + 1 or len(highs) < n or len(lows) < n:
            return 0.0
        trs = []
        for i in range(n - period, n):
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
            trs.append(tr)
        atr = sum(trs) / len(trs) if trs else 0.0
        return (atr / closes[-1] * 100) if closes[-1] else 0.0

    @staticmethod
    def _recent_vol_pct(closes, n=30):
        """Std-dev of the last ``n`` per-bar returns, as a percentage."""
        window = closes[-(n + 1):]
        rets = [window[j] / window[j - 1] - 1.0
                for j in range(1, len(window)) if window[j - 1]]
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        return var ** 0.5 * 100

    def _shortlist(self, scan_scores):
        """Best-scoring scanned names (plus anything we currently hold)."""
        scanned = sorted(scan_scores.items(), key=lambda x: x[1], reverse=True)
        short = [s for s, sc in scanned][: self.shortlist_n]
        for held in list(self.state["positions"]):   # snapshot (other thread)
            if held not in short:
                short.append(held)
        return short

    def self_update(self, retrain_ml):
        """Heavy scan/optimize/ML. Runs in the background thread — all network
        and CPU work is done lock-free; only the short final apply takes the
        lock so the fast trading loop is never blocked."""
        tag = "back-testing + retraining ML" if retrain_ml else "back-testing"
        scan_updates, scan_drops = {}, []
        if self.auto_universe:
            if (time.time() - self._last_universe_ts) >= 24 * 3600:
                self.refresh_universe()
            scan_updates, scan_drops = self._scan_pass()
            merged = {k: v for k, v in self.state["scan_scores"].items()
                      if k not in scan_drops}
            merged.update(scan_updates)
            candidates = self._shortlist(merged)
        else:
            candidates = list(self.universe)
        log(f"🧠 Self-update ({tag}) — {len(candidates)} candidates"
            + (f" from {len(self.universe)} scanned" if self.auto_universe else ""))

        new_params, new_scores, new_ml = {}, {}, {}
        scored = []
        for symbol in candidates:
            try:
                highs, lows, closes, vols = self.ex.ohlcv(
                    symbol, self.interval, self.history)
            except Exception as e:
                log(f"   {symbol}: data error {e}")
                continue
            if len(closes) < 80:
                continue
            params, metrics = optimize_symbol(closes)
            new_params[symbol] = params
            new_scores[symbol] = metrics
            if retrain_ml:
                # volume + high/low now feed the model alongside price.
                new_ml[symbol] = ml_model.train(symbol, closes, highs, lows, vols)
            scored.append((symbol, metrics["score"]))
            log(f"   {symbol}: score={metrics['score']} "
                f"ret={metrics['return_pct']}% win={metrics['win_rate']}% "
                f"fast={params['fast']} slow={params['slow']}")

        # Rank by back-test score, but PREFER names whose ML model has a proven
        # out-of-sample edge (>= MIN_EDGE) and demote those without one, so the
        # active slots land on names where the ML buy-filter actually works.
        def _edge(sym):
            acc = new_ml.get(sym)
            if acc is None:
                acc = self.state["ml_acc"].get(sym)
            if acc is None:                      # ML unavailable -> stay neutral
                return 0.0
            return (acc - ml_model.MIN_EDGE) * EDGE_WEIGHT
        scored.sort(key=lambda x: x[1] + _edge(x[0]), reverse=True)
        ranked = [s for s, sc in scored if sc > 0][: self.top_n]

        # ---- atomic apply (short critical section) ----
        with self._lock:
            for s in scan_drops:
                self.state["scan_scores"].pop(s, None)
            self.state["scan_scores"].update(scan_updates)
            self.state["params"].update(new_params)
            self.state["scores"].update(new_scores)
            if retrain_ml:
                self.state["ml_acc"].update(new_ml)
            for held in self.state["positions"]:           # keep held names
                if held not in ranked:
                    ranked.append(held)
            self.state["active"] = ranked
            self.state["last_optimize"] = iso()
            self.candidates = candidates
            keep = set(candidates) | set(self.state["positions"])
            for d in (self.state["params"], self.state["scores"],
                      self.state["ml_acc"]):
                for k in [k for k in d if k not in keep]:
                    d.pop(k, None)
            save_state(self.state)
        log(f"✅ Self-update done. Trading: {ranked or '(none profitable)'}")
        # Observe relative strength on every active name (not just at buy
        # moments) so the SMART_MONEY_MIN threshold can be tuned from real data.
        biases = []
        for sym in ranked:
            b = smart_money.long_short_bias(sym)
            biases.append(f"{sym} {b:.2f}" if b is not None else f"{sym} n/a")
        if biases:
            log("📊 relative-strength vs S&P (active): " + ", ".join(biases))

    # ------------------------------ trading ------------------------------
    def open_position(self, symbol, price):
        if len(self.state["positions"]) >= self.max_open:
            return
        # Escape check: don't enter a name whose spread is too wide to exit clean.
        if self.max_spread > 0:
            sp = self.ex.spread_pct(symbol)
            if sp is not None and sp > self.max_spread:
                log(f"⏸️  {symbol} entry skipped — spread {sp:.2f}% > "
                    f"{self.max_spread}% (can't escape clean)")
                return
        quote = self.quote_per_trade * self.regime.get("risk_multiplier", 1.0)
        fill, qty = self.ex.buy(symbol, quote, price)   # network (lock-free)
        with self._lock:
            self.state["positions"][symbol] = {
                "entry_price": fill, "qty": qty, "opened": iso(), "peak": fill}
            if self.mode == "dryrun":
                self.state["equity"] -= fill * qty
            record_trade(self.state, "BUY", symbol, fill, qty, self.mode, "signal")
        log(f"🟢 BUY {symbol} {qty:.6f} @ {fill:.4f}")

    def close_position(self, symbol, price, reason):
        pos = self.state["positions"].get(symbol)
        if not pos:
            return
        try:
            fill, qty = self.ex.sell(symbol, pos["qty"], price)   # network
        except RuntimeError as e:
            # nothing left to sell (already gone / dust) — stop tracking it
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
        params = merge_params(self.state["params"].get(symbol))
        # Fixed stop-loss / take-profit overrides (force values, ignore optimizer)
        if self.force_sl:
            params["stop_loss_pct"] = self.force_sl
        if self.force_tp:
            params["take_profit_pct"] = self.force_tp
        # One klines call gives OHLC + VOLUME; volume/high/low feed the ML model
        # (new learning signals) and the ATR volatility stop.
        highs, lows, closes, vols = self.ex.ohlcv(symbol, self.interval, self.history)
        price = closes[-1]
        prices[symbol] = price
        signal = latest_signal(closes, params)
        ml_prob = ml_model.predict_up(symbol, closes, highs, lows, vols)

        pos = self.state["positions"].get(symbol)
        if pos:
            entry = pos["entry_price"]
            pos["peak"] = max(pos.get("peak", entry), price)   # track high
            change = (price / entry - 1) * 100 if entry else 0
            # ATR stop: widen the stop on volatile names so normal noise doesn't
            # whipsaw us out. Effective stop = max(tuned %, ATR×mult). Opt-in.
            stop_pct = params["stop_loss_pct"]
            if self.atr_stop_mult > 0:
                atr_pct = self._atr_pct(highs, lows, closes, self.atr_period)
                if atr_pct > 0:
                    stop_pct = max(stop_pct or 0, atr_pct * self.atr_stop_mult)
            reason = None
            if stop_pct and change <= -stop_pct:
                reason = f"stop-loss {change:.1f}%"
            elif self.trailing_pct and pos["peak"] > entry and \
                    price <= pos["peak"] * (1 - self.trailing_pct / 100):
                drop = (price / pos["peak"] - 1) * 100
                reason = f"trailing stop ({drop:.1f}% from peak, P/L {change:+.1f}%)"
            elif not self.trailing_pct and params["take_profit_pct"] \
                    and change >= params["take_profit_pct"]:
                reason = f"take-profit {change:.1f}%"
            elif signal == "sell":
                reason = "trend exit"
            if reason:
                self.close_position(symbol, price, reason)
        elif self.pause_trading:
            # Trading paused: open no new positions (open ones are still managed
            # and exited by the block above).
            if signal == "buy":
                self._skip_log(symbol, "trading paused (PAUSE_TRADING=true)")
        else:
            ml_ok = (ml_prob is None) or (ml_prob >= self.ml_threshold)
            # The news gate can veto buys on strong negative news / panic VIX.
            regime_ok = (not self.news_gate) or self.regime.get("allow_buys", True)
            trend_ok = (not self.trend_filter) or self.market_bull
            if signal == "buy" and ml_ok and regime_ok and trend_ok:
                # Relative-strength + market-session checks: fetched only now (a
                # buy is otherwise approved) and cached, so they never touch the
                # fast exit loop.
                bias = smart_money.long_short_bias(symbol)
                if bias is not None:
                    log(f"📊 {symbol} relative strength vs S&P {bias:.2f} "
                        f"(min {self.smart_min})")
                snap = derivatives.snapshot(symbol)
                if snap["reason"] != "—":
                    log(f"🏛️  market session/VIX — {snap['reason']}")
                if self.smart_gate and bias is not None and bias < self.smart_min:
                    self._skip_log(symbol, f"lagging the market "
                                   f"(RS {bias:.2f} < {self.smart_min})")
                elif self.deriv_gate and not snap["confirm_long"]:
                    self._skip_log(symbol, f"market-risk veto — {snap['reason']}")
                else:
                    self.open_position(symbol, price)
            elif signal == "buy" and not trend_ok:
                self._skip_log(symbol, f"market downtrend ({self.market_trend_reason})")
            elif signal == "buy" and not regime_ok:
                self._skip_log(symbol, "best-practices: "
                               f"{self.regime.get('reason')}")
            elif signal == "buy" and not ml_ok:
                self._skip_log(symbol, f"ML {ml_prob} < {self.ml_threshold}")

    def _skip_log(self, symbol, msg):
        """Log a skipped-entry reason at most once per 60s per symbol."""
        now = time.time()
        if now - self._last_skip_log.get(symbol, 0) >= 60:
            log(f"⏸️  {symbol} buy skipped — {msg}")
            self._last_skip_log[symbol] = now

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
                day_pnl = (self.state["realized_pnl"]
                           - self.state["day_start_realized"])
                if day_pnl <= -abs(self.daily_loss_limit) \
                        and not self.state["halted"]:
                    self.state["halted"] = True
                    hit = True
        if hit:
            log("🛑 Daily loss limit hit. Pausing new entries until tomorrow.")

    # ------------------------- fast trading loop -------------------------
    def _handle_manual_sells(self):
        """Execute any 'بيع' button requests from the web monitor: market-sell
        the position now, regardless of strategy signal. Safe if the symbol has
        no open position (ignored)."""
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
        """Fast loop body — runs every POLL_SECONDS in the main thread. Only
        manages open/active positions (exits + entries) and writes the
        dashboard. The heavy scan/optimize runs in a background thread."""
        self._handle_manual_sells()
        prices = {}
        for symbol in list(self.state.get("active", [])):
            try:
                if self.state.get("halted"):
                    if symbol in self.state["positions"]:
                        closes = self.ex.closes(symbol, self.interval, self.history)
                        prices[symbol] = closes[-1]
                        self.close_position(symbol, closes[-1], "daily halt")
                    continue
                self.manage_symbol(symbol, prices)
            except Exception as e:
                log(f"   {symbol}: error {e}")

        with self._lock:
            save_state(self.state)
            try:
                dashboard.write_snapshot(
                    self.mode, self.candidates, self.state["active"],
                    self.state["params"], self.state["positions"],
                    self.state["scores"], self.state["ml_acc"],
                    self.state["trades"], self.state["equity"],
                    self.state["realized_pnl"], self.state["last_optimize"],
                    prices, regime=self.regime, account=self.account,
                    learning={"realtime": self.realtime,
                              "poll_seconds": self.poll_seconds,
                              "learn_seconds": self.learn_seconds})
            except Exception as e:
                log(f"dashboard write error: {e}")

    # ----------------------- background worker -----------------------
    def background_loop(self):
        """Runs the slow/heavy work off the trading path: balance, market
        regime, the scan/optimize/ML, and publishing — each on its own cadence.
        Never blocks the fast exit loop (only short, locked applies touch state)."""
        while True:
            try:
                self.check_daily_limit()
                self._refresh_balance()
                self._refresh_regime()
                self._refresh_market_trend()
                now_ts = time.time()
                learn_interval = self.learn_seconds if self.realtime \
                    else self.optimize_hours * 3600
                if not self.state.get("active") or \
                        (now_ts - self._last_opt_ts) >= learn_interval:
                    retrain = (now_ts - self._last_ml_ts) >= self.ml_retrain_min * 60
                    self.self_update(retrain)
                    self._last_opt_ts = now_ts
                    if retrain:
                        self._last_ml_ts = now_ts
                self._publish()
            except Exception as e:
                log(f"background error: {e}")
            time.sleep(3)

    def _refresh_balance(self):
        if (time.time() - self._last_bal_ts) < 60:
            return
        try:
            summ = self.ex.account_summary()
        except Exception as e:
            summ = None
            if self.mode in ("live", "testnet"):
                log(f"⚠️  balance read error: {e} — usually a missing 'Reading' "
                    "permission or an IP restriction on the API key")
        if summ:
            with self._lock:
                self.account = summ
                self.state["equity"] = summ["total_usdt"]
            log(f"💰 balance: {summ['total_usdt']} USDT total, "
                f"{summ['free_usdt']} USDT free")
        self._last_bal_ts = time.time()

    def _refresh_market_trend(self):
        """Update the broad-market bull/bear flag (S&P 500 vs its long MA)."""
        if not self.trend_filter:
            self.market_bull = True
            return
        if (time.time() - self._last_trend_ts) < 120:
            return
        self._last_trend_ts = time.time()
        try:
            t = market.index_trend(self.trend_ma)
            bull = t["bull"]
            if bull != self.market_bull:
                log(f"🧭 market trend → "
                    f"{'BULL (longs on)' if bull else 'BEAR (longs paused)'}: "
                    f"{t['reason']}")
            self.market_bull = bull
            self.market_trend_reason = t["reason"]
        except Exception as e:
            log(f"market-trend error: {e}")
            self.market_bull = True

    def _refresh_regime(self):
        if (time.time() - self._last_regime_ts) < 120:
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
        publish.backup_state(self.gh_repo, self.pub_branch, self.gh_token,
                             STATE_FILE)
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
        log(f"Bot started — mode={self.mode}, universe={uni}, "
            f"interval={self.interval}, TOP_N={self.top_n}, "
            f"{self.quote_per_trade} USDT/trade, poll={self.poll_seconds}s, "
            f"learn every {self.learn_seconds}s (background)")
        if self.mode == "live":
            log("⚠️  LIVE MODE — trading REAL money. Ctrl+C to stop.")

        # one synchronous learn so there's something to trade immediately
        try:
            self.self_update((time.time() - self._last_ml_ts)
                             >= self.ml_retrain_min * 60)
            self._last_opt_ts = self._last_ml_ts = time.time()
        except Exception as e:
            log(f"initial learn error: {e}")

        if "--once" in sys.argv:
            self.manage_cycle()
            return

        # heavy work in the background; fast exit loop in the foreground
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
