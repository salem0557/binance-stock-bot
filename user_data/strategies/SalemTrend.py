# SalemTrend v2 - spot, long only, 4h trend following
# Buys breakouts to a 40-candle high while Bitcoin is in an uptrend,
# exits when price breaks the 10-candle low or Bitcoin's trend turns down.
from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy, merge_informative_pair


class SalemTrend(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "4h"
    can_short = False
    startup_candle_count = 210

    # No fixed take-profit: let winners run, exit on trend break
    minimal_roi = {"0": 100}

    # Hard stop loss -10%
    stoploss = -0.10
    trailing_stop = False

    use_exit_signal = True
    exit_profit_only = False
    process_only_new_candles = True

    entry_len = 40   # breakout above highest high of last 40 candles (~7 days)
    exit_len = 10    # exit below lowest low of last 10 candles (~2 days)

    @property
    def protections(self):
        return [
            # Wait 1 candle before re-entering same pair
            {"method": "CooldownPeriod", "stop_duration_candles": 1},
            # 3 stop-losses within 2 days -> pause all trading 1 day
            {"method": "StoplossGuard", "lookback_period_candles": 12,
             "trade_limit": 3, "stop_duration_candles": 6, "only_per_pair": False},
        ]

    def informative_pairs(self):
        return [("BTC/USDT", "1d")]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["hh"] = dataframe["high"].rolling(self.entry_len).max().shift(1)
        dataframe["ll"] = dataframe["low"].rolling(self.exit_len).min().shift(1)

        # Market regime: Bitcoin daily close above its daily EMA50
        btc = self.dp.get_pair_dataframe("BTC/USDT", "1d").copy()
        btc["regime"] = (btc["close"] > ta.EMA(btc, timeperiod=50)).astype(int)
        dataframe = merge_informative_pair(
            dataframe, btc[["date", "regime"]], self.timeframe, "1d", ffill=True)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["regime_1d"] == 1)                 # Bitcoin uptrend
                & (dataframe["close"] > dataframe["hh"])      # breakout
                & (dataframe["close"] > dataframe["ema200"])  # pair uptrend
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["close"] < dataframe["ll"])        # trend broken
                | (dataframe["regime_1d"] == 0)               # Bitcoin turned down
            ),
            "exit_long",
        ] = 1
        return dataframe
