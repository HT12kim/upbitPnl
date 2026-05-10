"""
XRP 5분봉 변동성 돌파 전략 — 거래빈도 증가 그리드 서치

v3 winner는 1년 63건 (월 5건)으로 단기 검증 분산이 큼.
시간/요일 필터와 신호 임계값을 완화해 1년 ≥500건 (6m ≥250건)을
달성하면서 Sharpe·수익률은 유지/향상하는 후보를 탐색한다.

entry_kind = volatility_breakout 고정 (anchor).

환경변수:
  SMOKE=1      : 각 축 2개 값만 사용하는 빠른 구조 검증
  MARKET=KRW-XRP (기본값)
  WINDOW=6m    : 평가 구간 (6m | 1y | 3m), 기본 6m
  MIN_TRIPS=N  : 1차 필터 거래수 하한 오버라이드
  MIN_RETURN=R : 1차 필터 수익률(%) 하한 오버라이드
  MAX_MDD=M    : 1차 필터 MDD 하한 오버라이드 (음수, 예: -8.0)
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

SMOKE  = os.environ.get("SMOKE", "0") == "1"
MARKET = os.environ.get("MARKET", "KRW-XRP")
WINDOW = os.environ.get("WINDOW", "6m").lower()

_WINDOW_DAYS = {"3m": 91, "6m": 182, "1y": 365}
if WINDOW not in _WINDOW_DAYS:
    raise ValueError(f"WINDOW은 3m/6m/1y 중 하나여야 합니다. 받은 값: {WINDOW}")
WINDOW_DAYS = _WINDOW_DAYS[WINDOW]

_coin = MARKET.replace("KRW-", "").lower()
OUTPUT_PATH = Path(f"data_cache/{_coin}_more_trades_grid_{WINDOW}.csv")

# 1차 필터 기본값 (WINDOW에 따라 자동 스케일)
_default_min_trips = {"3m": 125, "6m": 250, "1y": 500}[WINDOW]
_default_min_ret   = {"3m": 7.5, "6m": 15.0, "1y": 25.0}[WINDOW]

MIN_TRIPS  = int(os.environ.get("MIN_TRIPS", _default_min_trips))
MIN_RETURN = float(os.environ.get("MIN_RETURN", _default_min_ret))
MAX_MDD    = float(os.environ.get("MAX_MDD", -8.0))   # 음수, 클수록 (0에 가까울수록) 안전

# ---------------------------------------------------------------------------
# 파라미터 그리드 (필터 완화 + 신호 임계값 완화)
# ---------------------------------------------------------------------------

if SMOKE:
    SESSIONS         = ["kr_night", "all"]
    WEEKDAYS         = ["weekday", "all"]
    VOL_MULTS        = [1.0, 1.5]
    ATR_MULTS        = [0.5, 1.5]
    BREAKOUT_LBACKS  = [10, 20]            # backtest 엔진 valid: [10, 20, 40]
    VOL_BASELINES    = [10]
    ATR_WINDOWS      = [14]
    MAX_HOLDS        = [24, 60]
    TAKE_PROFITS     = [2.0]
    STOP_LOSSES      = [1.5]
    TP1_PAIRS        = [(0.0, 0.0), (1.2, 0.7)]   # (tp1_pct, tp1_ratio)
else:
    SESSIONS         = ["kr_night", "all", "kr_day", "us_open"]
    WEEKDAYS         = ["weekday", "all"]
    VOL_MULTS        = [1.0, 1.2, 1.3, 1.5]
    ATR_MULTS        = [0.5, 1.0, 1.2, 1.5]
    BREAKOUT_LBACKS  = [10, 20, 40]        # backtest 엔진 valid: [10, 20, 40]
    VOL_BASELINES    = [10, 20]
    ATR_WINDOWS      = [14]
    MAX_HOLDS        = [24, 36, 48, 60]
    TAKE_PROFITS     = [1.5, 2.0]
    STOP_LOSSES      = [1.0, 1.5]
    TP1_PAIRS        = [(0.0, 0.0), (1.2, 0.7)]

# ---------------------------------------------------------------------------
# 결과 row 빌더
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
    progress_every = 500
    total = (len(SESSIONS) * len(WEEKDAYS) * len(VOL_MULTS) * len(ATR_MULTS) *
             len(BREAKOUT_LBACKS) * len(VOL_BASELINES) * len(ATR_WINDOWS) *
             len(MAX_HOLDS) * len(TAKE_PROFITS) * len(STOP_LOSSES) * len(TP1_PAIRS))
    t_start = time.perf_counter()
    for i, (session, weekday, vmult, amult, lb, vbase, aw, hold, tp, sl, tp1_pair) in enumerate(
        itertools.product(
            SESSIONS, WEEKDAYS, VOL_MULTS, ATR_MULTS, BREAKOUT_LBACKS,
            VOL_BASELINES, ATR_WINDOWS, MAX_HOLDS, TAKE_PROFITS, STOP_LOSSES, TP1_PAIRS,
        )
    ):
        tp1_pct, tp1_ratio = tp1_pair
        params = ShortTermParams(
            entry_kind="volatility_breakout",
            breakout_volume_mult=vmult,
            atr_breakout_mult=amult,
            take_profit_pct=tp,
            stop_loss_pct=sl,
            max_hold_bars=hold,
            trail_atr_mult=0.0,
            session_filter=session,
            weekday_filter=weekday,
            atr_window=aw,
            volume_baseline_window=vbase,
            breakout_lookback=lb,
            tp1_pct=tp1_pct,
            tp1_ratio=tp1_ratio if tp1_pct > 0 else 0.5,
        )
        rows.append(_build_row(run_short_term_backtest(prepared_df, params, context)))
        if (i + 1) % progress_every == 0:
            elapsed = time.perf_counter() - t_start
            rate = (i + 1) / elapsed
            eta = (total - i - 1) / rate
            print(f"  진행 {i+1:>5,}/{total:,} ({(i+1)/total*100:>5.1f}%)  "
                  f"속도={rate:.1f}건/s  ETA={eta:.0f}s")
    return rows


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------

_PARAM_COLS = [
    "session_filter", "weekday_filter",
    "breakout_volume_mult", "atr_breakout_mult", "breakout_lookback",
    "volume_baseline_window", "atr_window",
    "max_hold_bars", "take_profit_pct", "stop_loss_pct",
    "tp1_pct", "tp1_ratio",
]
_METRIC_COLS = [
    "return_pct", "sharpe_per_bar", "max_drawdown_pct", "profit_factor",
    "win_rate_pct", "completed_round_trips", "exposure_pct", "avg_hold_bars",
]


def print_top_n(df: pd.DataFrame, title: str, sort_col: str, ascending: bool, n: int = 20) -> None:
    print(f"\n{'=' * 100}")
    print(f"  {title}")
    print(f"{'=' * 100}")
    if sort_col not in df.columns or len(df) == 0:
        print(f"  데이터 없음 또는 컬럼 없음: {sort_col}")
        return
    top = df.sort_values(sort_col, ascending=ascending).head(n)
    display_cols = [c for c in _PARAM_COLS + _METRIC_COLS if c in top.columns]
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 240)
    pd.set_option("display.float_format", "{:.3f}".format)
    print(top[display_cols].to_string(index=False))


# ---------------------------------------------------------------------------
# 데이터 윈도우 필터
# ---------------------------------------------------------------------------

def slice_window(prepared_df: pd.DataFrame, window_days: int) -> pd.DataFrame:
    """전체 데이터에서 가장 최근 window_days 일치를 잘라 반환."""
    dt = prepared_df["datetime_kst"]
    cutoff = dt.max() - pd.Timedelta(days=window_days)
    return prepared_df[dt >= cutoff].copy().reset_index(drop=True)


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def optimize() -> pd.DataFrame:
    mode_label = "[SMOKE]" if SMOKE else "[FULL]"
    print(f"=== XRP 거래빈도 증가 그리드  {MARKET}  WINDOW={WINDOW} ({WINDOW_DAYS}d)  {mode_label} ===\n")

    t_total = time.perf_counter()

    full_df = load_prepared_market_data(market=MARKET, days=LOOKBACK_DAYS, use_cache=True)
    print(f"전체 데이터 로드: {len(full_df):,}캔들  "
          f"({full_df['datetime_kst'].iloc[0].date()} ~ {full_df['datetime_kst'].iloc[-1].date()})")

    prepared_df = slice_window(full_df, WINDOW_DAYS)
    print(f"평가 윈도우  : {len(prepared_df):,}캔들  "
          f"({prepared_df['datetime_kst'].iloc[0].date()} ~ {prepared_df['datetime_kst'].iloc[-1].date()})")

    context = build_short_term_context(prepared_df)
    print(f"컨텍스트 빌드 완료 ({time.perf_counter() - t_total:.1f}s)\n")

    total_combos = (len(SESSIONS) * len(WEEKDAYS) * len(VOL_MULTS) * len(ATR_MULTS) *
                    len(BREAKOUT_LBACKS) * len(VOL_BASELINES) * len(ATR_WINDOWS) *
                    len(MAX_HOLDS) * len(TAKE_PROFITS) * len(STOP_LOSSES) * len(TP1_PAIRS))
    print(f"총 조합 수: {total_combos:,}")

    t0 = time.perf_counter()
    rows = run_grid(prepared_df, context)
    elapsed = time.perf_counter() - t0
    ms_per = elapsed / max(len(rows), 1) * 1000
    print(f"\n그리드 완료: {len(rows):,}건  ({elapsed:.1f}s  /  {ms_per:.1f}ms 건)\n")

    return pd.DataFrame(rows)


if __name__ == "__main__":
    df = optimize()

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False)
    print(f"결과 저장: {OUTPUT_PATH}  ({len(df):,}건)")

    if len(df):
        bh = df["buy_and_hold_return_pct"].iloc[0]
        print(f"buy_and_hold_return_pct (참고): {bh:.2f}%\n")

    # 1차 필터
    print(f"\n{'=' * 100}")
    print(f"  1차 필터: trips ≥ {MIN_TRIPS}, return ≥ {MIN_RETURN}%, MDD ≥ {MAX_MDD}%")
    print(f"{'=' * 100}")
    filtered = df[
        (df["completed_round_trips"] >= MIN_TRIPS)
        & (df["return_pct"] >= MIN_RETURN)
        & (df["max_drawdown_pct"] >= MAX_MDD)
    ].copy()
    print(f"통과: {len(filtered):,}건 / {len(df):,}건")

    if len(filtered) == 0:
        print("\n⚠️  필터 통과 0건. 임계값 완화 필요. 참고로 grid 전체 상위 분포:")
        # 필터 미통과여도 상위 분포는 보여줌
        print_top_n(df, "전체 grid: trips DESC TOP 20", "completed_round_trips", ascending=False, n=20)
        print_top_n(df, "전체 grid: sharpe DESC TOP 20", "sharpe_per_bar",       ascending=False, n=20)
        sys.exit(0)

    # 2차 정렬: Sharpe DESC, Trips DESC, Return DESC
    print_top_n(filtered, "[FILTERED] sharpe_per_bar DESC TOP 30",
                "sharpe_per_bar", ascending=False, n=30)
    print_top_n(filtered, "[FILTERED] return_pct DESC TOP 20",
                "return_pct", ascending=False, n=20)
    print_top_n(filtered, "[FILTERED] completed_round_trips DESC TOP 20",
                "completed_round_trips", ascending=False, n=20)
