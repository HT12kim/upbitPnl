import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests

from trading.trading_strategy2 import _calculate_bollinger, _calculate_macd, _calculate_rsi


UPBIT_MINUTE_CANDLE_URL = "https://api.upbit.com/v1/candles/minutes/5"
MARKET = "KRW-ETH"
INITIAL_CASH = 1_000_000
FEE_RATE = 0.0005
MIN_ORDER_KRW = 5_000
LOOKBACK_DAYS = 365
CACHE_DIR = Path("data_cache")


@dataclass(frozen=True)
class StrategyParams:
    stop_loss_pct: float = 0.6942
    entry_ema_mode: str = "all"
    exit_ema_mode: str = "5_below_10"
    rebound_volume_multiplier: float = 0.0
    breakout_volume_multiplier: float = 1.0


@dataclass
class BacktestTrade:
    side: str
    signal_time: str
    execution_time: str
    price: float
    quantity: float
    cash_flow: float
    pnl: float = 0.0


def get_cache_path(market: str, days: int) -> Path:
    safe_market = market.replace("-", "_")
    return CACHE_DIR / f"{safe_market}_{days}d_5m.csv"


def fetch_minute_candles_for_period(market: str, days: int, use_cache: bool = True) -> pd.DataFrame:
    CACHE_DIR.mkdir(exist_ok=True)
    cache_path = get_cache_path(market, days)

    if use_cache and cache_path.exists():
        cached_df = pd.read_csv(cache_path)
        cached_df["datetime_kst"] = pd.to_datetime(cached_df["datetime_kst"], format="%Y-%m-%d %H:%M:%S")
        return cached_df

    target_end = datetime.now()
    target_start = target_end - timedelta(days=days)
    last_time_utc: Optional[str] = None
    all_frames: list[pd.DataFrame] = []

    while True:
        params: dict[str, Any] = {"market": market, "count": 200}
        if last_time_utc:
            params["to"] = last_time_utc

        response = requests.get(UPBIT_MINUTE_CANDLE_URL, params=params, timeout=10)
        response.raise_for_status()
        rows = response.json()
        if not rows:
            break

        frame = pd.DataFrame(rows)
        frame["date"] = frame["candle_date_time_kst"].str.split("T").str[0]
        frame["time"] = frame["candle_date_time_kst"].str.split("T").str[1]
        frame["open"] = frame["opening_price"]
        frame["close"] = frame["trade_price"]
        frame["high"] = frame["high_price"]
        frame["low"] = frame["low_price"]
        frame["volume"] = frame["candle_acc_trade_volume"]
        frame["datetime_kst"] = pd.to_datetime(frame["candle_date_time_kst"], format="%Y-%m-%dT%H:%M:%S")

        all_frames.append(frame)
        last_time_utc = frame["candle_date_time_utc"].iloc[-1]

        if frame["datetime_kst"].min().to_pydatetime() <= target_start:
            break

        # 공개 API 호출 빈도를 낮춰 차단 가능성을 줄입니다.
        time.sleep(0.12)

    if not all_frames:
        raise ValueError("백테스트용 캔들 데이터를 가져오지 못했습니다.")

    df = pd.concat(all_frames, ignore_index=True)
    df = df.sort_values("candle_date_time_kst").drop_duplicates(subset=["candle_date_time_kst"], keep="last")
    df = df[df["datetime_kst"] >= target_start].reset_index(drop=True)
    df.to_csv(cache_path, index=False)

    return df


def prepare_indicators(df: pd.DataFrame) -> pd.DataFrame:
    prepared = df.copy()

    prepared["MA20"] = prepared["close"].rolling(window=20).mean()
    prepared["MA200"] = prepared["close"].rolling(window=200).mean()
    prepared["MA200_slope"] = prepared["MA200"].diff()
    prepared["EMA5"] = prepared["close"].ewm(span=5, adjust=False).mean()
    prepared["EMA10"] = prepared["close"].ewm(span=10, adjust=False).mean()
    prepared["EMA20"] = prepared["close"].ewm(span=20, adjust=False).mean()
    prepared["EMA5_slope"] = prepared["EMA5"].diff()
    prepared["EMA10_slope"] = prepared["EMA10"].diff()
    prepared["EMA20_slope"] = prepared["EMA20"].diff()
    prepared["RSI"] = _calculate_rsi(prepared["close"], window=14)
    prepared["MACD"], prepared["MACD_signal"], prepared["MACD_histogram"] = _calculate_macd(prepared["close"])
    prepared["BB_upper"], prepared["BB_mid"], prepared["BB_lower"] = _calculate_bollinger(prepared["close"])
    prepared["BB_range"] = prepared["BB_upper"] - prepared["BB_lower"]
    prepared["candle_size"] = prepared["close"] - prepared["open"]
    prepared["is_big_bull"] = (prepared["candle_size"] > prepared["BB_range"] / 2) & (prepared["close"] > prepared["open"])
    prepared["Volume_MA20"] = prepared["volume"].rolling(window=20).mean()
    prepared["datetime"] = pd.to_datetime(prepared["date"] + " " + prepared["time"], format="%Y-%m-%d %H:%M:%S")

    return prepared


def load_prepared_market_data(market: str = MARKET, days: int = LOOKBACK_DAYS, use_cache: bool = True) -> pd.DataFrame:
    raw_df = fetch_minute_candles_for_period(market=market, days=days, use_cache=use_cache)
    return prepare_indicators(raw_df)


def calculate_max_drawdown(equity_curve: list[float]) -> float:
    peak = float("-inf")
    max_drawdown = 0.0

    for equity in equity_curve:
        peak = max(peak, equity)
        if peak <= 0:
            continue
        drawdown = (equity - peak) / peak
        max_drawdown = min(max_drawdown, drawdown)

    return max_drawdown


def build_backtest_context(prepared_df: pd.DataFrame) -> dict[str, Any]:
    context_df = prepared_df.copy()

    context_df["recent_candle_below_bb"] = (
        (context_df["close"] < context_df["BB_lower"]).rolling(window=20, min_periods=1).max().fillna(0).astype(bool)
    )
    context_df["rsi_under_30"] = (
        (context_df["RSI"] < 30).rolling(window=20, min_periods=1).max().fillna(0).astype(bool)
    )
    context_df["volume_ratio"] = (context_df["volume"] / context_df["Volume_MA20"]).replace([math.inf, -math.inf], 0).fillna(0)
    context_df["entry_slope_all"] = (
        (context_df["EMA5_slope"] > 0) &
        (context_df["EMA10_slope"] > 0) &
        (context_df["EMA20_slope"] > 0)
    )
    context_df["entry_slope_fast"] = (
        (context_df["EMA5_slope"] > 0) &
        (context_df["EMA10_slope"] > 0)
    )
    context_df["entry_slope_ema5_only"] = context_df["EMA5_slope"] > 0
    context_df["exit_cross_5_below_10"] = (
        (context_df["EMA5"].shift(1) > context_df["EMA10"].shift(1)) &
        (context_df["EMA5"] < context_df["EMA10"])
    )
    context_df["exit_cross_5_below_20"] = (
        (context_df["EMA5"].shift(1) > context_df["EMA20"].shift(1)) &
        (context_df["EMA5"] < context_df["EMA20"])
    )
    context_df["exit_cross_10_below_20"] = (
        (context_df["EMA10"].shift(1) > context_df["EMA20"].shift(1)) &
        (context_df["EMA10"] < context_df["EMA20"])
    )

    return {
        "signal_time": (context_df["date"] + " " + context_df["time"]).to_numpy(),
        "next_signal_time": (context_df["date"].shift(-1).fillna(context_df["date"]) + " " + context_df["time"].shift(-1).fillna(context_df["time"])).to_numpy(),
        "open": context_df["open"].astype(float).to_numpy(),
        "close": context_df["close"].astype(float).to_numpy(),
        "is_big_bull": context_df["is_big_bull"].fillna(False).astype(bool).to_numpy(),
        "positive_200ma_slope": (context_df["MA200_slope"] > 0).fillna(False).to_numpy(),
        "recent_candle_below_bb": context_df["recent_candle_below_bb"].to_numpy(),
        "rsi_under_30": context_df["rsi_under_30"].to_numpy(),
        "volume_ratio": context_df["volume_ratio"].to_numpy(),
        "entry_slope_all": context_df["entry_slope_all"].fillna(False).to_numpy(),
        "entry_slope_fast": context_df["entry_slope_fast"].fillna(False).to_numpy(),
        "entry_slope_ema5_only": context_df["entry_slope_ema5_only"].fillna(False).to_numpy(),
        "exit_cross_5_below_10": context_df["exit_cross_5_below_10"].fillna(False).to_numpy(),
        "exit_cross_5_below_20": context_df["exit_cross_5_below_20"].fillna(False).to_numpy(),
        "exit_cross_10_below_20": context_df["exit_cross_10_below_20"].fillna(False).to_numpy(),
        "datetime_kst": context_df["datetime_kst"],
    }


def _entry_slope_key(entry_ema_mode: str) -> str:
    if entry_ema_mode == "all":
        return "entry_slope_all"
    if entry_ema_mode == "fast":
        return "entry_slope_fast"
    if entry_ema_mode == "ema5_only":
        return "entry_slope_ema5_only"

    raise ValueError(f"지원하지 않는 entry_ema_mode 입니다: {entry_ema_mode}")


def _exit_cross_key(exit_ema_mode: str) -> str:
    if exit_ema_mode == "5_below_10":
        return "exit_cross_5_below_10"
    if exit_ema_mode == "5_below_20":
        return "exit_cross_5_below_20"
    if exit_ema_mode == "10_below_20":
        return "exit_cross_10_below_20"

    raise ValueError(f"지원하지 않는 exit_ema_mode 입니다: {exit_ema_mode}")


def run_backtest(prepared_df: pd.DataFrame, params: StrategyParams, context: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    df = prepared_df
    context = context or build_backtest_context(prepared_df)
    entry_slope_key = _entry_slope_key(params.entry_ema_mode)
    exit_cross_key = _exit_cross_key(params.exit_ema_mode)

    cash: float = float(INITIAL_CASH)
    asset_qty: float = 0.0
    buy_price: Optional[float] = None
    buy_cost_krw: float = 0.0
    buy_signal_index: Optional[int] = None
    trades: list[BacktestTrade] = []
    equity_curve: list[float] = []

    for signal_index in range(200, len(df) - 1):
        signal_time = context["signal_time"][signal_index]
        execution_time = context["next_signal_time"][signal_index]
        execution_price = context["open"][signal_index + 1]
        current_close = context["close"][signal_index]

        recent_candle_below_bb = bool(context["recent_candle_below_bb"][signal_index])
        rsi_under_30 = bool(context["rsi_under_30"][signal_index])
        is_positive_entry_slope = bool(context[entry_slope_key][signal_index])
        is_positive_200ma_slope = bool(context["positive_200ma_slope"][signal_index])
        volume_ratio = float(context["volume_ratio"][signal_index])
        breakout_volume_ok = volume_ratio >= params.breakout_volume_multiplier
        rebound_volume_ok = params.rebound_volume_multiplier <= 0 or (volume_ratio >= params.rebound_volume_multiplier)

        buy_signal = False
        if recent_candle_below_bb and is_positive_entry_slope and rebound_volume_ok:
            if is_positive_200ma_slope or rsi_under_30:
                buy_signal = True

        if not buy_signal and bool(context["is_big_bull"][signal_index]) and breakout_volume_ok:
            buy_signal = True

        sell_signal = False
        if asset_qty > 0 and buy_price is not None and buy_signal_index is not None:
            stop_loss_price = buy_price * (1 - (params.stop_loss_pct / 100))
            if current_close < stop_loss_price:
                sell_signal = True
            else:
                has_minimum_hold_bars = (signal_index - buy_signal_index) >= 2
                if has_minimum_hold_bars and bool(context[exit_cross_key][signal_index]):
                    sell_signal = True

        if asset_qty == 0 and buy_signal:
            order_krw = math.floor(cash * 0.999)
            if order_krw >= MIN_ORDER_KRW:
                asset_qty = (order_krw * (1 - FEE_RATE)) / execution_price
                cash -= order_krw
                buy_price = execution_price
                buy_signal_index = signal_index
                buy_cost_krw = float(order_krw)
                trades.append(
                    BacktestTrade(
                        side="buy",
                        signal_time=signal_time,
                        execution_time=execution_time,
                        price=execution_price,
                        quantity=asset_qty,
                        cash_flow=-buy_cost_krw,
                    )
                )
        elif asset_qty > 0 and sell_signal:
            sell_krw = asset_qty * execution_price * (1 - FEE_RATE)
            pnl = sell_krw - buy_cost_krw
            cash += sell_krw
            trades.append(
                BacktestTrade(
                    side="sell",
                    signal_time=signal_time,
                    execution_time=execution_time,
                    price=execution_price,
                    quantity=asset_qty,
                    cash_flow=sell_krw,
                    pnl=pnl,
                )
            )
            asset_qty = 0.0
            buy_price = None
            buy_cost_krw = 0.0
            buy_signal_index = None

        equity_curve.append(cash + (asset_qty * current_close))

    final_close = float(df["close"].iloc[-1])
    final_equity = cash + (asset_qty * final_close)
    completed_sells = [trade for trade in trades if trade.side == "sell"]
    win_count = sum(1 for trade in completed_sells if trade.pnl > 0)
    lose_count = sum(1 for trade in completed_sells if trade.pnl <= 0)
    win_rate = (win_count / len(completed_sells) * 100) if completed_sells else 0.0
    max_drawdown = calculate_max_drawdown(equity_curve) * 100
    buy_and_hold_return = (
        ((float(df["close"].iloc[-1]) / float(df["open"].iloc[0])) * (1 - FEE_RATE) * (1 - FEE_RATE) - 1) * 100
    )

    return {
        "params": asdict(params),
        "market": MARKET,
        "from": context["datetime_kst"].iloc[0].strftime("%Y-%m-%d %H:%M:%S"),
        "to": context["datetime_kst"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S"),
        "candles": len(df),
        "initial_cash": INITIAL_CASH,
        "final_equity": round(final_equity, 2),
        "return_pct": round(((final_equity / INITIAL_CASH) - 1) * 100, 2),
        "buy_and_hold_return_pct": round(buy_and_hold_return, 2),
        "completed_round_trips": len(completed_sells),
        "win_rate_pct": round(win_rate, 2),
        "wins": win_count,
        "losses": lose_count,
        "max_drawdown_pct": round(max_drawdown, 2),
        "open_position": asset_qty > 0,
        "last_mark_price": round(final_close, 2),
        "trade_log_preview": [
            {
                "side": trade.side,
                "signal_time": trade.signal_time,
                "execution_time": trade.execution_time,
                "price": round(trade.price, 2),
                "quantity": round(trade.quantity, 8),
                "cash_flow": round(trade.cash_flow, 2),
                "pnl": round(trade.pnl, 2),
            }
            for trade in trades[-10:]
        ],
    }


def backtest(params: Optional[StrategyParams] = None, use_cache: bool = True) -> dict[str, Any]:
    prepared_df = load_prepared_market_data(market=MARKET, days=LOOKBACK_DAYS, use_cache=use_cache)
    return run_backtest(prepared_df=prepared_df, params=params or StrategyParams())


if __name__ == "__main__":
    result = backtest()
    for key, value in result.items():
        print(f"{key}: {value}")
