"""
5분봉 초단기 매매 전략 그리드 서치 최적화

entry_kind 별로 파라미터 공간을 분리해 4회 순차 탐색.
결과는 data_cache/{coin}_short_term_optimization.csv 에 저장.

환경변수:
  SMOKE=1          : 각 축 2개 값만 사용하는 빠른 smoke test (구조 검증용)
  MARKET=KRW-XRP   : 대상 마켓 변경 (기본값: KRW-ETH)
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

from backtest_eth_krw import LOOKBACK_DAYS, MARKET as _DEFAULT_MARKET, load_prepared_market_data
from backtest_short_term_5m import (
    ShortTermParams,
    build_short_term_context,
    run_short_term_backtest,
)

SMOKE = os.environ.get("SMOKE", "0") == "1"
MARKET = os.environ.get("MARKET", _DEFAULT_MARKET)
_coin = MARKET.replace("KRW-", "").lower()
OUTPUT_PATH = Path(f"data_cache/{_coin}_short_term_optimization.csv")

# ---------------------------------------------------------------------------
# 파라미터 그리드 (SMOKE 모드에서 각 축 2개로 축소)
# ---------------------------------------------------------------------------

if SMOKE:
    TAKE_PROFIT_VALUES    = [0.4, 1.5]
    STOP_LOSS_VALUES      = [0.4, 1.5]
    MAX_HOLD_VALUES       = [6, 24]
    TRAIL_ATR_VALUES      = [0.0, 1.0]
    SESSION_VALUES        = ["all", "kr_day"]
    WEEKDAY_VALUES        = ["all", "weekday"]
    EMA_PAIRS             = [(5, 20), (10, 30)]
    SLOPE_LOOKBACK_VALUES = [1, 3]
    RSI_THRESHOLD_VALUES  = [25, 35]
    BB_STD_VALUES         = [2.0, 2.5]
    VOL_MULT_VALUES       = [1.0, 2.0]
    ATR_MULT_VALUES       = [0.5, 1.5]
else:
    TAKE_PROFIT_VALUES    = [0.4, 0.8, 1.5, 2.5]
    STOP_LOSS_VALUES      = [0.4, 0.8, 1.5]
    MAX_HOLD_VALUES       = [3, 6, 12, 24]
    TRAIL_ATR_VALUES      = [0.0, 1.0, 2.0]
    SESSION_VALUES        = ["all", "kr_day", "us_open", "kr_night"]
    WEEKDAY_VALUES        = ["all", "weekday", "weekend"]
    EMA_PAIRS             = [(5, 20), (8, 21), (10, 30)]
    SLOPE_LOOKBACK_VALUES = [1, 3]
    RSI_THRESHOLD_VALUES  = [25, 30, 35]
    BB_STD_VALUES         = [2.0, 2.5]
    VOL_MULT_VALUES       = [1.0, 1.5, 2.0]
    ATR_MULT_VALUES       = [0.5, 1.0, 1.5]


# ---------------------------------------------------------------------------
# 결과 row 빌더 (params 중첩 dict를 평탄화)
# ---------------------------------------------------------------------------

def _build_row(result: dict) -> dict:
    row: dict = {}
    row.update(result["params"])
    for k, v in result.items():
        if k not in ("params", "trade_log_preview", "market", "initial_cash"):
            row[k] = v
    return row


# ---------------------------------------------------------------------------
# entry_kind 별 그리드 실행 함수
# ---------------------------------------------------------------------------

def _run_grid(entry_kind: str, combos, combo_keys: list[str], prepared_df, context) -> list[dict]:
    rows: list[dict] = []
    for values in combos:
        kw = dict(zip(combo_keys, values))
        params = ShortTermParams(entry_kind=entry_kind, **kw)
        result = run_short_term_backtest(prepared_df, params, context)
        rows.append(_build_row(result))
    return rows


def run_grid_trend_pullback(prepared_df, context) -> list[dict]:
    combos = itertools.product(
        EMA_PAIRS,
        SLOPE_LOOKBACK_VALUES,
        TAKE_PROFIT_VALUES,
        STOP_LOSS_VALUES,
        MAX_HOLD_VALUES,
        TRAIL_ATR_VALUES,
        SESSION_VALUES,
        WEEKDAY_VALUES,
    )
    rows: list[dict] = []
    for ema_pair, slope_lb, tp, sl, hold, trail, session, weekday in combos:
        params = ShortTermParams(
            entry_kind="trend_pullback",
            ema_fast=ema_pair[0],
            ema_slow=ema_pair[1],
            slope_lookback=slope_lb,
            take_profit_pct=tp,
            stop_loss_pct=sl,
            max_hold_bars=hold,
            trail_atr_mult=trail,
            session_filter=session,
            weekday_filter=weekday,
        )
        rows.append(_build_row(run_short_term_backtest(prepared_df, params, context)))
    return rows


def run_grid_mean_reversion(prepared_df, context) -> list[dict]:
    rows: list[dict] = []
    for rsi_thr, bb_std, tp, sl, hold, trail, session, weekday in itertools.product(
        RSI_THRESHOLD_VALUES,
        BB_STD_VALUES,
        TAKE_PROFIT_VALUES,
        STOP_LOSS_VALUES,
        MAX_HOLD_VALUES,
        TRAIL_ATR_VALUES,
        SESSION_VALUES,
        WEEKDAY_VALUES,
    ):
        params = ShortTermParams(
            entry_kind="mean_reversion",
            rsi_buy_threshold=rsi_thr,
            bb_std=bb_std,
            take_profit_pct=tp,
            stop_loss_pct=sl,
            max_hold_bars=hold,
            trail_atr_mult=trail,
            session_filter=session,
            weekday_filter=weekday,
        )
        rows.append(_build_row(run_short_term_backtest(prepared_df, params, context)))
    return rows


def run_grid_volatility_breakout(prepared_df, context) -> list[dict]:
    rows: list[dict] = []
    for vol_mult, atr_mult, tp, sl, hold, trail, session, weekday in itertools.product(
        VOL_MULT_VALUES,
        ATR_MULT_VALUES,
        TAKE_PROFIT_VALUES,
        STOP_LOSS_VALUES,
        MAX_HOLD_VALUES,
        TRAIL_ATR_VALUES,
        SESSION_VALUES,
        WEEKDAY_VALUES,
    ):
        params = ShortTermParams(
            entry_kind="volatility_breakout",
            breakout_volume_mult=vol_mult,
            atr_breakout_mult=atr_mult,
            take_profit_pct=tp,
            stop_loss_pct=sl,
            max_hold_bars=hold,
            trail_atr_mult=trail,
            session_filter=session,
            weekday_filter=weekday,
        )
        rows.append(_build_row(run_short_term_backtest(prepared_df, params, context)))
    return rows


def run_grid_combo_or(prepared_df, context) -> list[dict]:
    """combo_or: 진입 파라미터는 기본값 고정, 청산/필터 축만 탐색."""
    rows: list[dict] = []
    for tp, sl, hold, trail, session, weekday in itertools.product(
        TAKE_PROFIT_VALUES,
        STOP_LOSS_VALUES,
        MAX_HOLD_VALUES,
        TRAIL_ATR_VALUES,
        SESSION_VALUES,
        WEEKDAY_VALUES,
    ):
        params = ShortTermParams(
            entry_kind="combo_or",
            take_profit_pct=tp,
            stop_loss_pct=sl,
            max_hold_bars=hold,
            trail_atr_mult=trail,
            session_filter=session,
            weekday_filter=weekday,
        )
        rows.append(_build_row(run_short_term_backtest(prepared_df, params, context)))
    return rows


# ---------------------------------------------------------------------------
# 결과 출력 헬퍼
# ---------------------------------------------------------------------------

_PARAM_COLS = [
    "entry_kind", "ema_fast", "ema_slow", "slope_lookback",
    "rsi_buy_threshold", "bb_std", "breakout_volume_mult", "atr_breakout_mult",
    "take_profit_pct", "stop_loss_pct", "max_hold_bars", "trail_atr_mult",
    "session_filter", "weekday_filter",
]
_METRIC_COLS = [
    "return_pct", "sharpe_per_bar", "max_drawdown_pct", "profit_factor",
    "win_rate_pct", "completed_round_trips", "exposure_pct",
    "avg_hold_bars", "cagr_pct",
]


def print_top_n(df: pd.DataFrame, title: str, sort_col: str, ascending: bool, n: int = 20) -> None:
    print(f"\n{'=' * 80}")
    print(f"  {title}")
    print(f"{'=' * 80}")
    if sort_col not in df.columns:
        print(f"  컬럼 없음: {sort_col}")
        return
    top = df.sort_values(sort_col, ascending=ascending).head(n)
    display_cols = [c for c in _PARAM_COLS + _METRIC_COLS if c in top.columns]
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", "{:.4f}".format)
    print(top[display_cols].to_string(index=False))


# ---------------------------------------------------------------------------
# 메인 최적화 함수
# ---------------------------------------------------------------------------

def optimize() -> pd.DataFrame:
    mode_label = "[SMOKE TEST]" if SMOKE else "[FULL GRID]"
    print(f"=== 5분봉 초단기 {MARKET} 매매 전략 최적화 {mode_label} ===\n")

    t_total = time.perf_counter()

    prepared_df = load_prepared_market_data(market=MARKET, days=LOOKBACK_DAYS, use_cache=True)
    print(f"데이터 로드 완료: {len(prepared_df):,}개 캔들")

    context = build_short_term_context(prepared_df)
    print(f"컨텍스트 빌드 완료 ({time.perf_counter() - t_total:.1f}s)\n")

    all_rows: list[dict] = []

    runners = [
        ("trend_pullback",      run_grid_trend_pullback),
        ("mean_reversion",      run_grid_mean_reversion),
        ("volatility_breakout", run_grid_volatility_breakout),
        ("combo_or",            run_grid_combo_or),
    ]

    for label, runner in runners:
        t0 = time.perf_counter()
        print(f"[{label}] 실행 중...", end=" ", flush=True)
        rows = runner(prepared_df, context)
        elapsed = time.perf_counter() - t0
        ms_per = elapsed / max(len(rows), 1) * 1000
        print(f"{len(rows):,}건 완료  ({elapsed:.1f}s  /  {ms_per:.1f}ms 건)")
        all_rows.extend(rows)

    total_elapsed = time.perf_counter() - t_total
    print(f"\n총 {len(all_rows):,}건  ({total_elapsed:.1f}s)\n")

    return pd.DataFrame(all_rows)


# ---------------------------------------------------------------------------
# 직접 실행
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    df = optimize()

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False)
    print(f"결과 저장: {OUTPUT_PATH}  ({len(df):,}건)")

    if len(df):
        bh = df["buy_and_hold_return_pct"].iloc[0]
        print(f"buy_and_hold_return_pct (참고): {bh:.2f}%\n")

    # max_drawdown_pct 는 음수 (e.g. -15.5 = MDD 15.5%)
    # DESC = 0에 가까운 쪽이 먼저 = MDD 작은 순
    print_top_n(df, "TOP 20  return_pct (DESC)",        "return_pct",        ascending=False)
    print_top_n(df, "TOP 20  sharpe_per_bar (DESC)",    "sharpe_per_bar",    ascending=False)
    print_top_n(df, "TOP 20  max_drawdown_pct (DESC, MDD 작은 순)", "max_drawdown_pct", ascending=False)
    print_top_n(df, "TOP 20  profit_factor (DESC)",     "profit_factor",     ascending=False)
