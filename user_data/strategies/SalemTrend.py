# SalemTrend - spot, long only, 1h
# Buys pullbacks inside an uptrend, exits on strength / trend break.
from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from technical import qtpylib


class SalemTrend(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    startup_candle_count = 210

    # Take profit table (minutes -> profit)
    minimal_roi = {"0": 0.08, "720": 0.04, "1440": 0.02, "2880": 0}

    # Hard stop loss -6%, trailing after +3%
    stoploss = -0.06
    trailing_stop = True
    trailing_stop_positive = 0.015
    trailing_stop_positive_offset = 0.03
    trailing_only_offset_is_reached = True

    use_exit_signal = True
    exit_profit_only = False
    process_only_new_candles = True

    @property
    def protections(self):
        return [
            # Wait 2 candles before re-entering same pair
            {"method": "CooldownPeriod", "stop_duration_candles": 2},
            # 3 stop-losses within 24h -> pause all trading 12h
            {"method": "StoplossGuard", "lookback_period_candles": 24,
             "trade_limit": 3, "stop_duration_candles": 12, "only_per_pair": False},
            # Daily drawdown > 8% -> pause 24h
            {"method": "MaxDrawdown", "lookback_period_candles": 24,
             "trade_limit": 3, "stop_duration_candles": 24, "max_allowed_drawdown": 0.08},
        ]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["ema50"] > dataframe["ema200"])          # uptrend
                & (dataframe["close"] > dataframe["ema200"])
                & qtpylib.crossed_above(dataframe["rsi"], 35)       # pullback ends
                & (dataframe["volume"] > dataframe["vol_ma"] * 0.8)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["rsi"] > 75)                              # overbought
                | (dataframe["close"] < dataframe["ema200"] * 0.98)  # trend broken
            ),
            "exit_long",
        ] = 1
        return dataframe
