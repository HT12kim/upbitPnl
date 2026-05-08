"""
5분봉 초단기 변동성 돌파 전략 그리드 서치 v2 (zoom-in)

기존 그리드(v1)에서 winner params가 모두 최댓값 끝에 있었으므로
탐색 범위를 상향 확장하고 atr_window / vol_baseline / breakout_lookback 축을 추가한다.

entry_kind = volatility_breakout 전용,  session=kr_night / weekday=weekday 고정.

환경변수:
  SMOKE=1        : 각 축 2개 값만 사용하는 빠른 구조 검증
  MARKET=KRW-XRP : 대상 마켓 변경 (기본값: KRW-ETH)
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
MARKET = os.environ.get("MARKET", _DEFAULT_MARKET)
_coin  = MARKET.replace("KRW-", "").lower()
OUTPUT_PATH = Path(f"data_cache/{_coin}_short_term_v2.csv")

# ---------------------------------------------------------------------------
# 파라미터 그리드 (기존 끝단 너머로 확장 + 3개 신규 축)
# ---------------------------------------------------------------------------

if SMOKE:
    VOL_MULT_V2        = [1.5, 3.0]
    ATR_MULT_V2        = [1.0, 2.5]
    TAKE_PROFIT_V2     = [2.0, 4.0]
    STOP_LOSS_V2       = [0.8, 1.5]
    MAX_HOLD_V2        = [18, 48]
    TRAIL_ATR_V2       = [0.0, 2.0]
    ATR_WINDOWS_V2     = [10, 21]
    VOL_BASELINES_V2   = [10, 30]
    BREAKOUT_LBACKS_V2 = [10, 40]
else:
    VOL_MULT_V2        = [1.5, 2.0, 2.5, 3.0]      # v1 max=2.0 → 3.0까지 확장
    ATR_MULT_V2        = [1.0, 1.5, 2.0, 2.5]      # v1 max=1.5 → 2.5까지 확장
    TAKE_PROFIT_V2     = [2.0, 2.5, 3.0, 3.5]      # v1 max=2.5 → 3.5까지 확장
    STOP_LOSS_V2       = [0.8, 1.5]
    MAX_HOLD_V2        = [18, 24, 36]               # v1 max=24 → 36까지 확장
    TRAIL_ATR_V2       = [0.0, 2.0]
    ATR_WINDOWS_V2     = [10, 14, 21]               # 신규 축
    VOL_BASELINES_V2   = [10, 20, 30]               # 신규 축
    BREAKOUT_LBACKS_V2 = [10, 20, 40]               # 신규 축

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

def run_grid_v2(prepared_df, context) -> list[dict]:
    rows: list[dict] = []
    for (vol_mult, atr_mult, tp, sl, hold, trail,
         atr_w, vol_base, lb) in itertools.product(
        VOL_MULT_V2,
        ATR_MULT_V2,
        TAKE_PROFIT_V2,
        STOP_LOSS_V2,
        MAX_HOLD_V2,
        TRAIL_ATR_V2,
        ATR_WINDOWS_V2,
        VOL_BASELINES_V2,
        BREAKOUT_LBACKS_V2,
    ):
        params = ShortTermParams(
            entry_kind="volatility_breakout",
            breakout_volume_mult=vol_mult,
            atr_breakout_mult=atr_mult,
            take_profit_pct=tp,
            stop_loss_pct=sl,
            max_hold_bars=hold,
            trail_atr_mult=trail,
            session_filter="kr_night",
            weekday_filter="weekday",
            atr_window=atr_w,
            volume_baseline_window=vol_base,
            breakout_lookback=lb,
        )
        rows.append(_build_row(run_short_term_backtest(prepared_df, params, context)))
    return rows


# ---------------------------------------------------------------------------
# 결과 출력 헬퍼
# ---------------------------------------------------------------------------

_PARAM_COLS = [
    "entry_kind", "breakout_volume_mult", "atr_breakout_mult",
    "take_profit_pct", "stop_loss_pct", "max_hold_bars", "trail_atr_mult",
    "atr_window", "volume_baseline_window", "breakout_lookback",
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
    pd.set_option("display.width", 220)
    pd.set_option("display.float_format", "{:.4f}".format)
    print(top[display_cols].to_string(index=False))


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def optimize() -> pd.DataFrame:
    mode_label = "[SMOKE TEST]" if SMOKE else "[FULL GRID v2]"
    print(f"=== 5분봉 volatility_breakout zoom-in {MARKET} {mode_label} ===\n")

    t_total = time.perf_counter()

    prepared_df = load_prepared_market_data(market=MARKET, days=LOOKBACK_DAYS, use_cache=True)
    print(f"데이터 로드 완료: {len(prepared_df):,}개 캔들")

    context = build_short_term_context(prepared_df)
    print(f"컨텍스트 빌드 완료 ({time.perf_counter() - t_total:.1f}s)\n")

    total_combos = (len(VOL_MULT_V2) * len(ATR_MULT_V2) * len(TAKE_PROFIT_V2) *
                    len(STOP_LOSS_V2) * len(MAX_HOLD_V2) * len(TRAIL_ATR_V2) *
                    len(ATR_WINDOWS_V2) * len(VOL_BASELINES_V2) * len(BREAKOUT_LBACKS_V2))
    print(f"총 조합 수: {total_combos:,}")

    t0 = time.perf_counter()
    rows = run_grid_v2(prepared_df, context)
    elapsed = time.perf_counter() - t0
    ms_per = elapsed / max(len(rows), 1) * 1000
    print(f"완료: {len(rows):,}건  ({elapsed:.1f}s  /  {ms_per:.1f}ms 건)\n")

    return pd.DataFrame(rows)


if __name__ == "__main__":
    df = optimize()

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False)
    print(f"결과 저장: {OUTPUT_PATH}  ({len(df):,}건)")

    if len(df):
        bh = df["buy_and_hold_return_pct"].iloc[0]
        print(f"buy_and_hold_return_pct (참고): {bh:.2f}%\n")

    print_top_n(df, "TOP 20  return_pct (DESC)",        "return_pct",        ascending=False)
    print_top_n(df, "TOP 20  sharpe_per_bar (DESC)",    "sharpe_per_bar",    ascending=False)
    print_top_n(df, "TOP 20  max_drawdown_pct (DESC, MDD 작은 순)", "max_drawdown_pct", ascending=False)
    print_top_n(df, "TOP 20  profit_factor (DESC)",     "profit_factor",     ascending=False)
