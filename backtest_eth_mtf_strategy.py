import math
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any

import pandas as pd

from backtest_eth_krw import FEE_RATE, INITIAL_CASH, LOOKBACK_DAYS, MARKET, MIN_ORDER_KRW, load_prepared_market_data
from backtest_eth_trend_strategy import prepare_trend_indicators


OUTPUT_PATH = Path("data_cache/eth_mtf_strategy_results.csv")


@dataclass(frozen=True)
class MTFStrategyParams:
    signal_span: int = 24
    stop_atr_mult: float = 1.8
    target_atr_mult: float = 3.6
    trailing_atr_mult: float = 1.8
    cooldown_bars: int = 12
    max_hold_bars: int = 72
    min_volume_ratio: float = 1.0
    rsi_reclaim_level: float = 50.0
    htf_pullback_window: int = 6


def _resample_timeframe(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    indexed = df.copy().set_index("datetime_kst")
    resampled = indexed.resample(rule, label="right", closed="right").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    resampled = resampled.dropna().reset_index()
    return resampled


def _build_htf_filter(base_df: pd.DataFrame, rule: str, fast_span: int, slow_span: int) -> pd.DataFrame:
    htf = _resample_timeframe(base_df, rule)
    htf["fast_ema"] = htf["close"].ewm(span=fast_span, adjust=False).mean()
    htf["slow_ema"] = htf["close"].ewm(span=slow_span, adjust=False).mean()
    htf["slow_slope"] = htf["slow_ema"].diff()
    htf["htf_bull"] = (
        (htf["fast_ema"] > htf["slow_ema"])
        & (htf["slow_slope"] > 0)
        & (htf["close"] > htf["fast_ema"])
    )
    return htf[["datetime_kst", "htf_bull"]]


def prepare_mtf_indicators(df: pd.DataFrame, params: MTFStrategyParams) -> pd.DataFrame:
    prepared = prepare_trend_indicators(df)

    filter_15m = _build_htf_filter(prepared, "15min", fast_span=32, slow_span=96)
    filter_1h = _build_htf_filter(prepared, "1h", fast_span=24, slow_span=72)
    filter_15m = filter_15m.rename(columns={"htf_bull": "bull_15m"})
    filter_1h = filter_1h.rename(columns={"htf_bull": "bull_1h"})

    merged = pd.merge_asof(
        prepared.sort_values("datetime_kst"),
        filter_15m.sort_values("datetime_kst"),
        on="datetime_kst",
        direction="backward",
    )
    merged = pd.merge_asof(
        merged.sort_values("datetime_kst"),
        filter_1h.sort_values("datetime_kst"),
        on="datetime_kst",
        direction="backward",
    )
    merged["bull_15m"] = merged["bull_15m"].where(merged["bull_15m"].notna(), False).astype(bool)
    merged["bull_1h"] = merged["bull_1h"].where(merged["bull_1h"].notna(), False).astype(bool)
    merged["mtf_bull"] = merged["bull_15m"] & merged["bull_1h"]

    return merged


def enrich_signal_columns(prepared: pd.DataFrame, params: MTFStrategyParams) -> pd.DataFrame:
    enriched = prepared.copy()
    enriched["signal_ema"] = enriched["close"].ewm(span=params.signal_span, adjust=False).mean()
    enriched["signal_ema_slope"] = enriched["signal_ema"].diff()
    enriched["recent_pullback"] = (
        ((enriched["low"] <= enriched["signal_ema"]) | (enriched["close"] <= enriched["BB_mid"]))
        .rolling(window=params.htf_pullback_window, min_periods=1)
        .max()
        .fillna(0)
        .astype(bool)
    )
    enriched["momentum_reclaim"] = (
        (enriched["close"] > enriched["signal_ema"])
        & (enriched["signal_ema_slope"] > 0)
        & (enriched["MACD_histogram"] > 0)
        & (enriched["MACD_histogram"] > enriched["MACD_histogram"].shift(1))
    )
    return enriched


def run_mtf_backtest(prepared_base: pd.DataFrame, params: MTFStrategyParams) -> dict[str, Any]:
    prepared = enrich_signal_columns(prepared_base, params)

    cash = float(INITIAL_CASH)
    asset_qty = 0.0
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
            and bool(signal_candle["mtf_bull"])
            and bool(signal_candle["recent_pullback"])
            and bool(signal_candle["momentum_reclaim"])
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
            elif not bool(signal_candle["mtf_bull"]):
                exit_signal = True
                exit_reason = "htf_trend_break"
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
                entry_cost = float(order_krw)
                stop_price = execution_price - (atr * params.stop_atr_mult)
                target_price = execution_price + (atr * params.target_atr_mult)
                highest_close = current_close
                hold_bars = 0
                trades.append(
                    {
                        "side": "buy",
                        "signal_time": signal_time,
                        "execution_time": execution_time,
                        "price": execution_price,
                        "quantity": asset_qty,
                        "reason": "mtf_trend_pullback_reclaim",
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


def search_mtf_strategy(prepared_base: pd.DataFrame) -> pd.DataFrame:
    signal_span_values = [18, 24]
    stop_atr_values = [1.8, 2.0]
    target_atr_values = [3.6, 4.2]
    trailing_atr_values = [1.8, 2.2]
    cooldown_values = [12, 24]
    max_hold_values = [72, 96]
    volume_values = [1.0, 1.05]
    rsi_values = [50.0, 52.0]

    results: list[dict[str, Any]] = []

    for signal_span, stop_atr_mult, target_atr_mult, trailing_atr_mult, cooldown_bars, max_hold_bars, min_volume_ratio, rsi_reclaim_level in product(
        signal_span_values,
        stop_atr_values,
        target_atr_values,
        trailing_atr_values,
        cooldown_values,
        max_hold_values,
        volume_values,
        rsi_values,
    ):
        params = MTFStrategyParams(
            signal_span=signal_span,
            stop_atr_mult=stop_atr_mult,
            target_atr_mult=target_atr_mult,
            trailing_atr_mult=trailing_atr_mult,
            cooldown_bars=cooldown_bars,
            max_hold_bars=max_hold_bars,
            min_volume_ratio=min_volume_ratio,
            rsi_reclaim_level=rsi_reclaim_level,
        )
        result = run_mtf_backtest(prepared_base, params)
        results.append(
            {
                "signal_span": signal_span,
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
    base_df = load_prepared_market_data(market=MARKET, days=LOOKBACK_DAYS, use_cache=True)
    prepared_base = prepare_mtf_indicators(base_df, MTFStrategyParams())
    results_df = search_mtf_strategy(prepared_base)
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    results_df.to_csv(OUTPUT_PATH, index=False)

    best_row = results_df.iloc[0]
    best_params = MTFStrategyParams(
        signal_span=int(best_row["signal_span"]),
        stop_atr_mult=float(best_row["stop_atr_mult"]),
        target_atr_mult=float(best_row["target_atr_mult"]),
        trailing_atr_mult=float(best_row["trailing_atr_mult"]),
        cooldown_bars=int(best_row["cooldown_bars"]),
        max_hold_bars=int(best_row["max_hold_bars"]),
        min_volume_ratio=float(best_row["min_volume_ratio"]),
        rsi_reclaim_level=float(best_row["rsi_reclaim_level"]),
    )
    best_result = run_mtf_backtest(prepared_base, best_params)

    print("top_10_by_return:")
    print(results_df.head(10).to_string(index=False))
    print("")
    print("best_result_detail:")
    for key, value in best_result.items():
        print(f"{key}: {value}")
