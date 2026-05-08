import math
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any

import pandas as pd

from backtest_eth_krw import FEE_RATE, INITIAL_CASH, LOOKBACK_DAYS, MARKET, MIN_ORDER_KRW, load_prepared_market_data


OUTPUT_PATH = Path("data_cache/eth_trend_strategy_results.csv")


@dataclass(frozen=True)
class TrendStrategyParams:
    trend_fast_span: int = 96
    trend_slow_span: int = 288
    signal_span: int = 24
    stop_atr_mult: float = 1.8
    target_atr_mult: float = 3.2
    trailing_atr_mult: float = 1.6
    cooldown_bars: int = 12
    max_hold_bars: int = 72
    min_volume_ratio: float = 1.05
    rsi_reclaim_level: float = 52.0


def prepare_trend_indicators(df: pd.DataFrame) -> pd.DataFrame:
    prepared = df.copy()

    prepared["EMA96"] = prepared["close"].ewm(span=96, adjust=False).mean()
    prepared["EMA288"] = prepared["close"].ewm(span=288, adjust=False).mean()
    prepared["EMA24"] = prepared["close"].ewm(span=24, adjust=False).mean()
    prepared["EMA96_slope"] = prepared["EMA96"].diff()
    prepared["EMA288_slope"] = prepared["EMA288"].diff()
    prepared["EMA24_slope"] = prepared["EMA24"].diff()

    prepared["prev_close"] = prepared["close"].shift(1)
    tr_components = pd.concat(
        [
            prepared["high"] - prepared["low"],
            (prepared["high"] - prepared["prev_close"]).abs(),
            (prepared["low"] - prepared["prev_close"]).abs(),
        ],
        axis=1,
    )
    prepared["TR"] = tr_components.max(axis=1)
    prepared["ATR14"] = prepared["TR"].rolling(window=14).mean()

    prepared["volume_ratio"] = (prepared["volume"] / prepared["Volume_MA20"]).fillna(0.0)
    prepared["rsi_prev"] = prepared["RSI"].shift(1)
    prepared["recent_pullback"] = (
        ((prepared["low"] <= prepared["EMA24"]) | (prepared["close"] <= prepared["BB_mid"]))
        .rolling(window=6, min_periods=1)
        .max()
        .fillna(0)
        .astype(bool)
    )
    prepared["bull_trend"] = (
        (prepared["EMA96"] > prepared["EMA288"])
        & (prepared["EMA288_slope"] > 0)
        & (prepared["close"] > prepared["EMA96"])
    )
    prepared["momentum_reclaim"] = (
        (prepared["close"] > prepared["EMA24"])
        & (prepared["EMA24_slope"] > 0)
        & (prepared["MACD_histogram"] > prepared["MACD_histogram"].shift(1))
    )

    return prepared


def run_trend_backtest(df: pd.DataFrame, params: TrendStrategyParams) -> dict[str, Any]:
    prepared = df.copy()

    # 파라미터별 EMA를 재계산합니다.
    prepared["trend_fast"] = prepared["close"].ewm(span=params.trend_fast_span, adjust=False).mean()
    prepared["trend_slow"] = prepared["close"].ewm(span=params.trend_slow_span, adjust=False).mean()
    prepared["trend_slow_slope"] = prepared["trend_slow"].diff()
    prepared["signal_ema"] = prepared["close"].ewm(span=params.signal_span, adjust=False).mean()
    prepared["signal_ema_slope"] = prepared["signal_ema"].diff()
    prepared["recent_pullback_param"] = (
        ((prepared["low"] <= prepared["signal_ema"]) | (prepared["close"] <= prepared["BB_mid"]))
        .rolling(window=6, min_periods=1)
        .max()
        .fillna(0)
        .astype(bool)
    )
    prepared["bull_trend_param"] = (
        (prepared["trend_fast"] > prepared["trend_slow"])
        & (prepared["trend_slow_slope"] > 0)
        & (prepared["close"] > prepared["trend_fast"])
    )
    prepared["momentum_reclaim_param"] = (
        (prepared["close"] > prepared["signal_ema"])
        & (prepared["signal_ema_slope"] > 0)
        & (prepared["MACD_histogram"] > prepared["MACD_histogram"].shift(1))
        & (prepared["MACD_histogram"] > 0)
    )

    cash = float(INITIAL_CASH)
    asset_qty = 0.0
    entry_price = 0.0
    entry_cost = 0.0
    stop_price = 0.0
    target_price = 0.0
    highest_close = 0.0
    hold_bars = 0
    cooldown_until = -1

    trades: list[dict[str, Any]] = []
    equity_curve: list[float] = []

    for signal_index in range(300, len(prepared) - 1):
        signal_candle = prepared.iloc[signal_index]
        next_candle = prepared.iloc[signal_index + 1]
        signal_time = f"{signal_candle['date']} {signal_candle['time']}"
        execution_time = f"{next_candle['date']} {next_candle['time']}"
        execution_price = float(next_candle["open"])
        current_close = float(signal_candle["close"])
        atr = float(signal_candle["ATR14"]) if not pd.isna(signal_candle["ATR14"]) else 0.0

        entry_signal = (
            signal_index > cooldown_until
            and bool(signal_candle["bull_trend_param"])
            and bool(signal_candle["recent_pullback_param"])
            and bool(signal_candle["momentum_reclaim_param"])
            and float(signal_candle["volume_ratio"]) >= params.min_volume_ratio
            and float(signal_candle["RSI"]) >= params.rsi_reclaim_level
            and float(signal_candle["rsi_prev"]) < params.rsi_reclaim_level
        )

        exit_signal = False
        exit_reason = ""

        if asset_qty > 0:
            hold_bars += 1
            highest_close = max(highest_close, current_close)
            trailing_stop = highest_close - (atr * params.trailing_atr_mult)

            if current_close <= stop_price:
                exit_signal = True
                exit_reason = "stop_loss"
            elif current_close >= target_price:
                exit_signal = True
                exit_reason = "take_profit"
            elif current_close <= trailing_stop:
                exit_signal = True
                exit_reason = "trailing_stop"
            elif current_close < float(signal_candle["signal_ema"]):
                exit_signal = True
                exit_reason = "signal_ema_break"
            elif hold_bars >= params.max_hold_bars:
                exit_signal = True
                exit_reason = "time_exit"

        if asset_qty == 0 and entry_signal and atr > 0:
            order_krw = math.floor(cash * 0.999)
            if order_krw >= MIN_ORDER_KRW:
                asset_qty = (order_krw * (1 - FEE_RATE)) / execution_price
                cash -= order_krw
                entry_price = execution_price
                entry_cost = float(order_krw)
                stop_price = entry_price - (atr * params.stop_atr_mult)
                target_price = entry_price + (atr * params.target_atr_mult)
                highest_close = current_close
                hold_bars = 0
                trades.append(
                    {
                        "side": "buy",
                        "signal_time": signal_time,
                        "execution_time": execution_time,
                        "price": execution_price,
                        "quantity": asset_qty,
                        "reason": "trend_pullback_reclaim",
                        "pnl": 0.0,
                    }
                )
        elif asset_qty > 0 and exit_signal:
            sell_krw = asset_qty * execution_price * (1 - FEE_RATE)
            pnl = sell_krw - entry_cost
            cash += sell_krw
            trades.append(
                {
                    "side": "sell",
                    "signal_time": signal_time,
                    "execution_time": execution_time,
                    "price": execution_price,
                    "quantity": asset_qty,
                    "reason": exit_reason,
                    "pnl": pnl,
                }
            )
            asset_qty = 0.0
            entry_price = 0.0
            entry_cost = 0.0
            stop_price = 0.0
            target_price = 0.0
            highest_close = 0.0
            hold_bars = 0
            cooldown_until = signal_index + params.cooldown_bars

        equity_curve.append(cash + (asset_qty * current_close))

    final_close = float(prepared["close"].iloc[-1])
    final_equity = cash + (asset_qty * final_close)
    sell_trades = [trade for trade in trades if trade["side"] == "sell"]
    wins = sum(1 for trade in sell_trades if trade["pnl"] > 0)
    losses = sum(1 for trade in sell_trades if trade["pnl"] <= 0)
    win_rate = (wins / len(sell_trades) * 100) if sell_trades else 0.0

    peak = float("-inf")
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = min(max_drawdown, (equity - peak) / peak)

    buy_and_hold_return = (
        ((float(prepared["close"].iloc[-1]) / float(prepared["open"].iloc[0])) * (1 - FEE_RATE) * (1 - FEE_RATE) - 1)
        * 100
    )

    return {
        "params": params,
        "market": MARKET,
        "from": prepared["datetime_kst"].iloc[0].strftime("%Y-%m-%d %H:%M:%S"),
        "to": prepared["datetime_kst"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S"),
        "candles": len(prepared),
        "final_equity": round(final_equity, 2),
        "return_pct": round(((final_equity / INITIAL_CASH) - 1) * 100, 2),
        "buy_and_hold_return_pct": round(buy_and_hold_return, 2),
        "completed_round_trips": len(sell_trades),
        "win_rate_pct": round(win_rate, 2),
        "wins": wins,
        "losses": losses,
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "open_position": asset_qty > 0,
        "trade_log_preview": trades[-10:],
    }


def search_trend_strategy(df: pd.DataFrame) -> pd.DataFrame:
    stop_atr_values = [1.6, 2.0]
    target_atr_values = [2.8, 3.6]
    trailing_atr_values = [1.4, 1.8]
    cooldown_values = [12, 24]
    max_hold_values = [48, 72]
    volume_values = [1.0, 1.1]
    rsi_values = [50.0, 55.0]

    results: list[dict[str, Any]] = []

    for stop_atr_mult, target_atr_mult, trailing_atr_mult, cooldown_bars, max_hold_bars, min_volume_ratio, rsi_reclaim_level in product(
        stop_atr_values,
        target_atr_values,
        trailing_atr_values,
        cooldown_values,
        max_hold_values,
        volume_values,
        rsi_values,
    ):
        params = TrendStrategyParams(
            stop_atr_mult=stop_atr_mult,
            target_atr_mult=target_atr_mult,
            trailing_atr_mult=trailing_atr_mult,
            cooldown_bars=cooldown_bars,
            max_hold_bars=max_hold_bars,
            min_volume_ratio=min_volume_ratio,
            rsi_reclaim_level=rsi_reclaim_level,
        )
        result = run_trend_backtest(df, params)
        results.append(
            {
                "stop_atr_mult": stop_atr_mult,
                "target_atr_mult": target_atr_mult,
                "trailing_atr_mult": trailing_atr_mult,
                "cooldown_bars": cooldown_bars,
                "max_hold_bars": max_hold_bars,
                "min_volume_ratio": min_volume_ratio,
                "rsi_reclaim_level": rsi_reclaim_level,
                "return_pct": result["return_pct"],
                "final_equity": result["final_equity"],
                "max_drawdown_pct": result["max_drawdown_pct"],
                "completed_round_trips": result["completed_round_trips"],
                "win_rate_pct": result["win_rate_pct"],
            }
        )

    result_df = pd.DataFrame(results)
    result_df.sort_values(
        by=["return_pct", "max_drawdown_pct", "completed_round_trips"],
        ascending=[False, False, True],
        inplace=True,
    )
    result_df.reset_index(drop=True, inplace=True)

    return result_df


if __name__ == "__main__":
    prepared_df = prepare_trend_indicators(load_prepared_market_data(market=MARKET, days=LOOKBACK_DAYS, use_cache=True))
    results_df = search_trend_strategy(prepared_df)
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    results_df.to_csv(OUTPUT_PATH, index=False)

    best_row = results_df.iloc[0]
    best_params = TrendStrategyParams(
        stop_atr_mult=float(best_row["stop_atr_mult"]),
        target_atr_mult=float(best_row["target_atr_mult"]),
        trailing_atr_mult=float(best_row["trailing_atr_mult"]),
        cooldown_bars=int(best_row["cooldown_bars"]),
        max_hold_bars=int(best_row["max_hold_bars"]),
        min_volume_ratio=float(best_row["min_volume_ratio"]),
        rsi_reclaim_level=float(best_row["rsi_reclaim_level"]),
    )
    best_result = run_trend_backtest(prepared_df, best_params)

    print("top_10_by_return:")
    print(results_df.head(10).to_string(index=False))
    print("")
    print("best_result_detail:")
    for key, value in best_result.items():
        print(f"{key}: {value}")
