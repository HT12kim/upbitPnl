"""
시장별(ETH/BTC) 별도 최적화: volatility_breakout 그리드 서치

session/weekday/max_hold/tp1 ladder 모두 그리드에 포함해 시장별 winner 탐색.

환경변수:
  MARKET=KRW-ETH | KRW-BTC  (필수)
  SMOKE=1                   (각 축 2값만)
  WINDOW=1y                 (기본 365일)
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
MARKET = os.environ.get("MARKET")
if not MARKET:
    raise SystemExit("MARKET 환경변수 필수 (KRW-ETH 또는 KRW-BTC)")
WINDOW = os.environ.get("WINDOW", "1y").lower()

_WINDOW_DAYS = {"3m": 91, "6m": 182, "1y": 365}
WINDOW_DAYS = _WINDOW_DAYS[WINDOW]

_coin = MARKET.replace("KRW-", "").lower()
OUTPUT_PATH = Path(f"data_cache/{_coin}_per_market_grid_{WINDOW}.csv")

# ---------------------------------------------------------------------------
# 그리드
# ---------------------------------------------------------------------------

if SMOKE:
    SESSIONS         = ["kr_night", "all"]
    WEEKDAYS         = ["weekday"]
    VOL_MULTS        = [1.5, 2.0]
    ATR_MULTS        = [1.0, 1.5]
    VOL_BASELINES    = [10]
    BREAKOUT_LBACKS  = [10]
    MAX_HOLDS        = [24, 60]
    TAKE_PROFITS     = [2.0]
    STOP_LOSSES      = [1.5]
    TP1_PAIRS        = [(0.0, 0.0), (1.2, 0.7)]
else:
    SESSIONS         = ["kr_night", "kr_day", "all"]
    WEEKDAYS         = ["weekday", "all"]
    VOL_MULTS        = [1.5, 2.0, 2.5]
    ATR_MULTS        = [1.0, 1.5, 2.0]
    VOL_BASELINES    = [10, 20]
    BREAKOUT_LBACKS  = [10, 20]
    MAX_HOLDS        = [24, 36, 48, 60]
    TAKE_PROFITS     = [1.5, 2.0, 2.5, 3.0]
    STOP_LOSSES      = [1.0, 1.5, 2.0]
    TP1_PAIRS        = [(0.0, 0.0), (1.2, 0.7)]


def _build_row(result: dict) -> dict:
    row: dict = {}
    row.update(result["params"])
    for k, v in result.items():
        if k not in ("params", "trade_log_preview", "market", "initial_cash"):
            row[k] = v
    return row


def run_grid(prepared_df, context) -> list[dict]:
    rows: list[dict] = []
    total = (len(SESSIONS) * len(WEEKDAYS) * len(VOL_MULTS) * len(ATR_MULTS) *
             len(VOL_BASELINES) * len(BREAKOUT_LBACKS) * len(MAX_HOLDS) *
             len(TAKE_PROFITS) * len(STOP_LOSSES) * len(TP1_PAIRS))
    t0 = time.perf_counter()
    progress_every = max(1, total // 20)
    for i, (ses, wkd, vmult, amult, vbase, lb, hold, tp, sl, tp1) in enumerate(
        itertools.product(
            SESSIONS, WEEKDAYS, VOL_MULTS, ATR_MULTS, VOL_BASELINES, BREAKOUT_LBACKS,
            MAX_HOLDS, TAKE_PROFITS, STOP_LOSSES, TP1_PAIRS,
        )
    ):
        tp1_pct, tp1_ratio = tp1
        params = ShortTermParams(
            entry_kind="volatility_breakout",
            breakout_volume_mult=vmult, atr_breakout_mult=amult,
            take_profit_pct=tp, stop_loss_pct=sl,
            max_hold_bars=hold, trail_atr_mult=0.0,
            session_filter=ses, weekday_filter=wkd,
            atr_window=14, volume_baseline_window=vbase, breakout_lookback=lb,
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


_PARAM_COLS = [
    "session_filter", "weekday_filter",
    "breakout_volume_mult", "atr_breakout_mult",
    "volume_baseline_window", "breakout_lookback",
    "max_hold_bars", "take_profit_pct", "stop_loss_pct",
    "tp1_pct", "tp1_ratio",
]
_METRIC_COLS = [
    "return_pct", "sharpe_per_bar", "max_drawdown_pct", "profit_factor",
    "win_rate_pct", "completed_round_trips", "exposure_pct", "avg_hold_bars",
]


def print_top_n(df, title, sort_col, ascending, n=20):
    print(f"\n{'=' * 110}")
    print(f"  {title}")
    print(f"{'=' * 110}")
    if sort_col not in df.columns or len(df) == 0:
        print("  데이터 없음")
        return
    top = df.sort_values(sort_col, ascending=ascending).head(n)
    cols = [c for c in _PARAM_COLS + _METRIC_COLS if c in top.columns]
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 250)
    pd.set_option("display.float_format", "{:.3f}".format)
    print(top[cols].to_string(index=False))


def slice_window(df, days):
    cutoff = df["datetime_kst"].max() - pd.Timedelta(days=days)
    return df[df["datetime_kst"] >= cutoff].copy().reset_index(drop=True)


def optimize():
    label = "[SMOKE]" if SMOKE else "[FULL]"
    print(f"=== 시장별 최적화  {MARKET}  WINDOW={WINDOW} ({WINDOW_DAYS}d)  {label} ===\n")

    full = load_prepared_market_data(market=MARKET, days=LOOKBACK_DAYS, use_cache=True)
    df = slice_window(full, WINDOW_DAYS) if WINDOW_DAYS < 365 else full.copy()
    print(f"평가 윈도우: {len(df):,}캔들  ({df['datetime_kst'].iloc[0].date()} ~ {df['datetime_kst'].iloc[-1].date()})")

    ctx = build_short_term_context(df)

    total = (len(SESSIONS) * len(WEEKDAYS) * len(VOL_MULTS) * len(ATR_MULTS) *
             len(VOL_BASELINES) * len(BREAKOUT_LBACKS) * len(MAX_HOLDS) *
             len(TAKE_PROFITS) * len(STOP_LOSSES) * len(TP1_PAIRS))
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

    pos = df[(df["return_pct"] > 0) & (df["completed_round_trips"] >= 30) &
             (df["max_drawdown_pct"] >= -15.0)].copy()
    print(f"\n[1차필터] return>0 AND trips≥30 AND MDD≥-15%  통과: {len(pos):,}건 / {len(df):,}건")

    if len(pos) == 0:
        print("\n⚠️ 필터 통과 0건. 전체 grid 상위:")
        print_top_n(df, "전체 sharpe DESC TOP 20", "sharpe_per_bar", False, 20)
        print_top_n(df, "전체 return DESC TOP 20", "return_pct", False, 20)
    else:
        print_top_n(pos, "[FILTERED] sharpe DESC TOP 30", "sharpe_per_bar", False, 30)
        print_top_n(pos, "[FILTERED] return DESC TOP 20", "return_pct", False, 20)
        print_top_n(pos, "[FILTERED] trips DESC TOP 20",  "completed_round_trips", False, 20)
