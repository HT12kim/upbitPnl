"""
시장별 1년 5분봉 다전략 그리드 서치.

목표:
  - 거래 빈도를 일정 수준 이상 확보
  - 단순 수익률 1위가 아니라 MDD/Profit Factor/Sharpe를 함께 검토
  - 결과 CSV를 select_strategy_winners.py에서 실거래 설정으로 변환

환경변수:
  MARKET=KRW-XRP | KRW-ETH | KRW-BTC
  WINDOW=1y | 6m | 3m
  SMOKE=1
"""

from __future__ import annotations

import itertools
import os
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

_root = Path(__file__).parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from backtest_eth_krw import LOOKBACK_DAYS, load_prepared_market_data
from backtest_short_term_5m import (
    ShortTermParams,
    build_short_term_context,
    run_short_term_backtest,
)

MARKET = os.environ.get("MARKET", "KRW-XRP")
WINDOW = os.environ.get("WINDOW", "1y").lower()
SMOKE = os.environ.get("SMOKE", "0") == "1"

WINDOW_DAYS_MAP = {"3m": 91, "6m": 182, "1y": 365}
if WINDOW not in WINDOW_DAYS_MAP:
    raise ValueError(f"WINDOW은 3m/6m/1y 중 하나여야 합니다. 받은 값: {WINDOW}")

WINDOW_DAYS = WINDOW_DAYS_MAP[WINDOW]
COIN = MARKET.replace("KRW-", "").lower()
OUTPUT_PATH = Path(f"data_cache/{COIN}_multi_strategy_grid_{WINDOW}.csv")

if SMOKE:
    ENTRY_KINDS = ["volatility_breakout", "trend_pullback", "vwap_pullback"]
    SESSIONS = ["all", "kr_night"]
    WEEKDAYS = ["all"]
    VOL_MULTS = [1.0, 1.5]
    ATR_MULTS = [0.5, 1.0]
    LBS = [10]
    VBASES = [10]
    HOLDS = [24, 48]
    TPS = [1.5]
    SLS = [1.0]
    TP1S = [(0.0, 0.5)]
else:
    ENTRY_KINDS = ["volatility_breakout", "trend_pullback", "vwap_pullback", "combo_or"]
    SESSIONS = ["all", "kr_night", "kr_day", "us_open"]
    WEEKDAYS = ["all", "weekday"]
    VOL_MULTS = [1.0, 1.2, 1.5, 2.0]
    ATR_MULTS = [0.5, 1.0, 1.2, 1.5]
    LBS = [10, 20, 40]
    VBASES = [10, 20]
    HOLDS = [24, 36, 48, 60]
    TPS = [1.5, 2.0, 2.5]
    SLS = [1.0, 1.5, 2.0]
    TP1S = [(0.0, 0.5), (1.2, 0.7)]


def _slice_window(df: pd.DataFrame, days: int) -> pd.DataFrame:
    cutoff = df["datetime_kst"].max() - pd.Timedelta(days=days)
    return df[df["datetime_kst"] >= cutoff].copy().reset_index(drop=True)


def _build_row(result: dict[str, Any]) -> dict[str, Any]:
    row = dict(result["params"])
    for key, value in result.items():
        if key not in {"params", "trade_log_preview", "market", "initial_cash"}:
            row[key] = value
    return row


def _params_from_grid(
    entry_kind: str,
    session: str,
    weekday: str,
    vol_mult: float,
    atr_mult: float,
    lb: int,
    vbase: int,
    hold: int,
    tp: float,
    sl: float,
    tp1: tuple[float, float],
) -> ShortTermParams:
    tp1_pct, tp1_ratio = tp1
    return ShortTermParams(
        entry_kind=entry_kind,
        breakout_volume_mult=vol_mult,
        atr_breakout_mult=atr_mult,
        take_profit_pct=tp,
        stop_loss_pct=sl,
        max_hold_bars=hold,
        trail_atr_mult=0.0,
        session_filter=session,
        weekday_filter=weekday,
        atr_window=14,
        volume_baseline_window=vbase,
        breakout_lookback=lb,
        tp1_pct=tp1_pct,
        tp1_ratio=tp1_ratio if tp1_pct > 0 else 0.5,
        vwap_volume_mult=vol_mult,
    )


def optimize() -> pd.DataFrame:
    label = "[SMOKE]" if SMOKE else "[FULL]"
    print(f"=== 다전략 거래빈도 최적화 {MARKET} WINDOW={WINDOW} {label} ===")

    full = load_prepared_market_data(market=MARKET, days=LOOKBACK_DAYS, use_cache=True)
    df = _slice_window(full, WINDOW_DAYS) if WINDOW_DAYS < 365 else full.copy()
    ctx = build_short_term_context(df)

    grid = itertools.product(
        ENTRY_KINDS,
        SESSIONS,
        WEEKDAYS,
        VOL_MULTS,
        ATR_MULTS,
        LBS,
        VBASES,
        HOLDS,
        TPS,
        SLS,
        TP1S,
    )
    total = (
        len(ENTRY_KINDS) * len(SESSIONS) * len(WEEKDAYS) * len(VOL_MULTS)
        * len(ATR_MULTS) * len(LBS) * len(VBASES) * len(HOLDS)
        * len(TPS) * len(SLS) * len(TP1S)
    )
    rows: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    progress_every = max(1, total // 20)

    for idx, values in enumerate(grid, start=1):
        params = _params_from_grid(*values)
        rows.append(_build_row(run_short_term_backtest(df, params, ctx)))
        if idx % progress_every == 0:
            elapsed = time.perf_counter() - t0
            rate = idx / elapsed if elapsed > 0 else 0.0
            eta = (total - idx) / rate if rate > 0 else 0.0
            print(f"  진행 {idx:,}/{total:,} ({idx / total * 100:5.1f}%) ETA={eta:.0f}s")

    result = pd.DataFrame(rows)
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    result.to_csv(OUTPUT_PATH, index=False)
    print(f"결과 저장: {OUTPUT_PATH} ({len(result):,}건)")
    return result


if __name__ == "__main__":
    df_result = optimize()
    if len(df_result) == 0:
        raise SystemExit(0)

    filtered = df_result[
        (df_result["return_pct"] > 0)
        & (df_result["completed_round_trips"] >= 30)
        & (df_result["max_drawdown_pct"] >= -15.0)
        & (df_result["profit_factor"] >= 1.0)
    ].copy()
    print(f"1차 필터 통과: {len(filtered):,}/{len(df_result):,}")
    if len(filtered):
        filtered["score"] = (
            filtered["return_pct"]
            + filtered["sharpe_per_bar"] * 2.0
            + filtered["completed_round_trips"] * 0.02
            + filtered["max_drawdown_pct"] * 1.5
        )
        cols = [
            "entry_kind", "session_filter", "weekday_filter",
            "breakout_volume_mult", "atr_breakout_mult", "breakout_lookback",
            "volume_baseline_window", "take_profit_pct", "stop_loss_pct",
            "max_hold_bars", "return_pct", "sharpe_per_bar",
            "max_drawdown_pct", "profit_factor", "completed_round_trips",
            "score",
        ]
        print(filtered.sort_values("score", ascending=False)[cols].head(20).to_string(index=False))
