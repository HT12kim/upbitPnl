"""
XRP 5분봉 EMA 눌림목(trend_pullback) 그리드 서치

trend_pullback 시그널은 volatility_breakout보다 5-25배 자주 발생하므로
청산 파라미터 (TP/SL/hold)가 v3와 달라야 한다. 그리드 서치로
양수 수익 + 충분한 거래수 + 안정적 Sharpe를 만족하는 조합을 찾는다.

환경변수:
  SMOKE=1    : 각 축 2개 값만, 빠른 구조 검증
  WINDOW=6m  : 평가 구간 (3m | 6m | 1y), 기본 6m
"""

import itertools
import os
import sys
import time
from pathlib import Path

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

SMOKE  = os.environ.get("SMOKE", "0") == "1"
MARKET = os.environ.get("MARKET", "KRW-XRP")
WINDOW = os.environ.get("WINDOW", "6m").lower()

_WINDOW_DAYS = {"3m": 91, "6m": 182, "1y": 365}
WINDOW_DAYS = _WINDOW_DAYS[WINDOW]

_coin = MARKET.replace("KRW-", "").lower()
OUTPUT_PATH = Path(f"data_cache/{_coin}_trend_pullback_{WINDOW}.csv")

# ---------------------------------------------------------------------------
# 파라미터 그리드 — trend_pullback 전용 축에 집중
# ---------------------------------------------------------------------------

if SMOKE:
    EMA_PAIRS        = [(5, 20), (8, 21)]
    SLOPE_LOOKBACKS  = [1]
    SESSIONS         = ["kr_night", "all"]
    WEEKDAYS         = ["all"]
    TAKE_PROFITS     = [0.6, 1.2]
    STOP_LOSSES      = [0.4, 0.8]
    MAX_HOLDS        = [12, 24]
    TRAIL_ATR        = [0.0, 1.0]
    TP1_PAIRS        = [(0.0, 0.0)]
else:
    EMA_PAIRS        = [(5, 20), (8, 21), (10, 30)]
    SLOPE_LOOKBACKS  = [1, 3]
    SESSIONS         = ["all", "kr_night", "kr_day"]
    WEEKDAYS         = ["all", "weekday"]
    TAKE_PROFITS     = [0.4, 0.6, 0.8, 1.2, 2.0]
    STOP_LOSSES      = [0.4, 0.6, 0.8, 1.2]
    MAX_HOLDS        = [6, 12, 24, 48]
    TRAIL_ATR        = [0.0, 1.0, 2.0]
    TP1_PAIRS        = [(0.0, 0.0), (0.4, 0.5)]   # 더 작은 부분익절


# ---------------------------------------------------------------------------
# 행 빌더
# ---------------------------------------------------------------------------

def _build_row(result: dict) -> dict:
    row: dict = {}
    row.update(result["params"])
    for k, v in result.items():
        if k not in ("params", "trade_log_preview", "market", "initial_cash"):
            row[k] = v
    return row


# ---------------------------------------------------------------------------
# 그리드 실행
# ---------------------------------------------------------------------------

def run_grid(prepared_df, context) -> list[dict]:
    rows: list[dict] = []
    total = (len(EMA_PAIRS) * len(SLOPE_LOOKBACKS) * len(SESSIONS) * len(WEEKDAYS) *
             len(TAKE_PROFITS) * len(STOP_LOSSES) * len(MAX_HOLDS) *
             len(TRAIL_ATR) * len(TP1_PAIRS))
    t0 = time.perf_counter()
    progress_every = max(1, total // 20)
    for i, (ema_pair, slope_lb, session, wkday, tp, sl, hold, trail, tp1_pair) in enumerate(
        itertools.product(
            EMA_PAIRS, SLOPE_LOOKBACKS, SESSIONS, WEEKDAYS,
            TAKE_PROFITS, STOP_LOSSES, MAX_HOLDS, TRAIL_ATR, TP1_PAIRS,
        )
    ):
        ema_fast, ema_slow = ema_pair
        tp1_pct, tp1_ratio = tp1_pair
        params = ShortTermParams(
            entry_kind="trend_pullback",
            ema_fast=ema_fast, ema_slow=ema_slow, slope_lookback=slope_lb,
            take_profit_pct=tp, stop_loss_pct=sl,
            max_hold_bars=hold, trail_atr_mult=trail,
            session_filter=session, weekday_filter=wkday,
            tp1_pct=tp1_pct, tp1_ratio=tp1_ratio if tp1_pct > 0 else 0.5,
        )
        rows.append(_build_row(run_short_term_backtest(prepared_df, params, context)))
        if (i + 1) % progress_every == 0:
            elapsed = time.perf_counter() - t0
            rate = (i + 1) / elapsed
            eta = (total - i - 1) / rate
            print(f"  진행 {i+1:>5,}/{total:,} ({(i+1)/total*100:>5.1f}%)  "
                  f"속도={rate:.1f}건/s  ETA={eta:.0f}s")
    return rows


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------

_PARAM_COLS = [
    "ema_fast", "ema_slow", "slope_lookback",
    "session_filter", "weekday_filter",
    "take_profit_pct", "stop_loss_pct", "max_hold_bars", "trail_atr_mult",
    "tp1_pct", "tp1_ratio",
]
_METRIC_COLS = [
    "return_pct", "sharpe_per_bar", "max_drawdown_pct", "profit_factor",
    "win_rate_pct", "completed_round_trips", "exposure_pct", "avg_hold_bars",
]


def print_top_n(df: pd.DataFrame, title: str, sort_col: str, ascending: bool, n: int = 20) -> None:
    print(f"\n{'=' * 110}")
    print(f"  {title}")
    print(f"{'=' * 110}")
    if sort_col not in df.columns or len(df) == 0:
        print(f"  데이터 없음")
        return
    top = df.sort_values(sort_col, ascending=ascending).head(n)
    cols = [c for c in _PARAM_COLS + _METRIC_COLS if c in top.columns]
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 250)
    pd.set_option("display.float_format", "{:.3f}".format)
    print(top[cols].to_string(index=False))


def slice_window(prepared_df: pd.DataFrame, window_days: int) -> pd.DataFrame:
    cutoff = prepared_df["datetime_kst"].max() - pd.Timedelta(days=window_days)
    return prepared_df[prepared_df["datetime_kst"] >= cutoff].copy().reset_index(drop=True)


def optimize() -> pd.DataFrame:
    label = "[SMOKE]" if SMOKE else "[FULL]"
    print(f"=== EMA 눌림목 그리드  {MARKET}  WINDOW={WINDOW} ({WINDOW_DAYS}d)  {label} ===\n")

    full = load_prepared_market_data(market=MARKET, days=LOOKBACK_DAYS, use_cache=True)
    df = slice_window(full, WINDOW_DAYS)
    print(f"평가 윈도우: {len(df):,}캔들  ({df['datetime_kst'].iloc[0].date()} ~ {df['datetime_kst'].iloc[-1].date()})")

    ctx = build_short_term_context(df)

    total = (len(EMA_PAIRS) * len(SLOPE_LOOKBACKS) * len(SESSIONS) * len(WEEKDAYS) *
             len(TAKE_PROFITS) * len(STOP_LOSSES) * len(MAX_HOLDS) *
             len(TRAIL_ATR) * len(TP1_PAIRS))
    print(f"총 조합 수: {total:,}\n")

    t0 = time.perf_counter()
    rows = run_grid(df, ctx)
    el = time.perf_counter() - t0
    print(f"\n그리드 완료: {len(rows):,}건  ({el:.1f}s)\n")

    return pd.DataFrame(rows)


if __name__ == "__main__":
    df = optimize()
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False)
    print(f"결과 저장: {OUTPUT_PATH}  ({len(df):,}건)")

    if len(df):
        bh = df["buy_and_hold_return_pct"].iloc[0]
        print(f"buy_and_hold_return_pct (참고): {bh:.2f}%")

    # 1차 필터: 양수 수익 + 거래수 충분 + MDD 한도
    pos = df[(df["return_pct"] > 0) & (df["completed_round_trips"] >= 30) &
             (df["max_drawdown_pct"] >= -8.0)].copy()
    print(f"\n[1차필터] return>0 AND trips≥30 AND MDD≥-8%  통과: {len(pos):,}건 / {len(df):,}건")

    if len(pos) == 0:
        print("\n⚠️ 필터 통과 0건. 전체 grid 상위 분포:")
        print_top_n(df, "전체 sharpe DESC TOP 30", "sharpe_per_bar", False, 30)
        print_top_n(df, "전체 return DESC TOP 30", "return_pct", False, 30)
    else:
        print_top_n(pos, "[FILTERED] sharpe DESC TOP 30", "sharpe_per_bar", False, 30)
        print_top_n(pos, "[FILTERED] return DESC TOP 30", "return_pct", False, 30)
        print_top_n(pos, "[FILTERED] trips DESC TOP 30",  "completed_round_trips", False, 30)
