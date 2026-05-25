"""
라이브 매매와 백테스트의 진입 판단 차이를 줄이기 위한 공통 시그널 모듈.

백테스트 엔진(backtest_short_term_5m.py)의 주요 entry_kind를 라이브 5분봉
DataFrame 기준으로 계산한다. 실거래에서는 마지막 완성 캔들(iloc[-2])만 판단한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class LiveSignalParams:
    entry_kind: str = "volatility_breakout"
    ema_fast: int = 5
    ema_slow: int = 20
    slope_lookback: int = 1
    rsi_buy_threshold: int = 30
    bb_std: float = 2.0
    breakout_volume_mult: float = 1.5
    atr_breakout_mult: float = 1.0
    atr_window: int = 14
    volume_baseline_window: int = 20
    breakout_lookback: int = 20
    vwap_window: int = 96
    vwap_pullback_lookback: int = 5
    vwap_volume_mult: float = 1.0


def _rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0, math.nan)
    return (100.0 - (100.0 / (1.0 + rs))).fillna(50.0)


def _bollinger_lower(close: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.Series:
    ma = close.rolling(window, min_periods=window).mean()
    std = close.rolling(window, min_periods=window).std()
    return ma - std * num_std


def evaluate_live_signal(df: pd.DataFrame, params: LiveSignalParams) -> dict[str, Any]:
    """
    마지막 완성 5분봉 기준 진입 시그널을 계산한다.

    반환값은 UI/로그/텔레그램에서 공통으로 쓸 수 있도록 원시 지표와
    entry_signal을 함께 담는다.
    """
    if len(df) < 3:
        raise ValueError("시그널 계산에는 최소 3개 이상의 캔들이 필요합니다.")

    close = df["close"].astype(float)
    open_ = df["open"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(
        alpha=1.0 / params.atr_window,
        adjust=False,
        min_periods=params.atr_window,
    ).mean()

    vol_ma = volume.rolling(params.volume_baseline_window, min_periods=1).mean()
    vol_ratio = (volume / vol_ma).replace([float("inf"), float("-inf")], 0).fillna(0)

    rolling_high_prev = high.shift(1).rolling(params.breakout_lookback, min_periods=1).max()
    breakout_threshold = rolling_high_prev + atr * params.atr_breakout_mult

    ema_fast = close.ewm(span=params.ema_fast, adjust=False).mean()
    ema_slow = close.ewm(span=params.ema_slow, adjust=False).mean()
    slope = ema_fast.diff(params.slope_lookback)
    pullback_5 = (close.shift(1) < ema_fast.shift(1)).rolling(5, min_periods=1).max().fillna(0).astype(bool)
    trend_signal = (
        (ema_fast > ema_slow)
        & pullback_5
        & (close > ema_fast)
        & (slope > 0)
    )

    rsi14 = _rsi(close)
    bb_lower = _bollinger_lower(close, num_std=params.bb_std)
    rsi_recent_under = (
        (rsi14 < params.rsi_buy_threshold)
        .rolling(5, min_periods=1)
        .max()
        .fillna(0)
        .astype(bool)
    )
    mean_reversion_signal = (
        (close.shift(1) < bb_lower)
        & (close > open_)
        & rsi_recent_under
    )

    volatility_signal = (
        (close > breakout_threshold)
        & (vol_ratio >= params.breakout_volume_mult)
    )

    pv = close * volume
    vwap = (
        pv.rolling(params.vwap_window, min_periods=20).sum()
        / volume.rolling(params.vwap_window, min_periods=20).sum()
    )
    vwap_touched = (
        (low <= vwap)
        .shift(1)
        .rolling(params.vwap_pullback_lookback, min_periods=1)
        .max()
        .fillna(0)
        .astype(bool)
    )
    vwap_signal = (
        (close > vwap)
        & vwap_touched
        & (close > open_)
        & (vol_ratio >= params.vwap_volume_mult)
    )

    kind = params.entry_kind
    if kind == "trend_pullback":
        selected_signal = trend_signal
    elif kind == "mean_reversion":
        selected_signal = mean_reversion_signal
    elif kind == "volatility_breakout":
        selected_signal = volatility_signal
    elif kind == "vwap_pullback":
        selected_signal = vwap_signal
    elif kind == "combo_or":
        selected_signal = trend_signal | mean_reversion_signal | volatility_signal
    else:
        raise ValueError(f"지원하지 않는 entry_kind: {kind}")

    i = -2
    candle_time = str(df["candle_date_time_kst"].iloc[i]) if "candle_date_time_kst" in df else str(df.index[i])
    entry_signal = bool(selected_signal.iloc[i]) if not pd.isna(selected_signal.iloc[i]) else False

    return {
        "entry_signal": entry_signal,
        "entry_kind": kind,
        "close": float(close.iloc[i]),
        "open": float(open_.iloc[i]),
        "vol_ratio": float(vol_ratio.iloc[i]),
        "bt": float(breakout_threshold.iloc[i]),
        "atr": float(atr.iloc[i]),
        "ema_fast": float(ema_fast.iloc[i]),
        "ema_slow": float(ema_slow.iloc[i]),
        "rsi": float(rsi14.iloc[i]),
        "bb_lower": float(bb_lower.iloc[i]) if not pd.isna(bb_lower.iloc[i]) else math.nan,
        "vwap": float(vwap.iloc[i]) if not pd.isna(vwap.iloc[i]) else math.nan,
        "trend_signal": bool(trend_signal.iloc[i]) if not pd.isna(trend_signal.iloc[i]) else False,
        "mean_reversion_signal": bool(mean_reversion_signal.iloc[i]) if not pd.isna(mean_reversion_signal.iloc[i]) else False,
        "volatility_signal": bool(volatility_signal.iloc[i]) if not pd.isna(volatility_signal.iloc[i]) else False,
        "vwap_signal": bool(vwap_signal.iloc[i]) if not pd.isna(vwap_signal.iloc[i]) else False,
        "candle_time": candle_time,
    }
