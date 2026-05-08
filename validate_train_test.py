"""
Train / Test 분할 검증

전체 기간을 Train(앞 9개월) / Test(뒤 3개월)로 나눠
v2 winner 파라미터가 out-of-sample에서도 유효한지 확인한다.

환경변수:
  MARKET=KRW-XRP : 대상 마켓 (기본: KRW-ETH)
"""

import os
import sys
import time
from pathlib import Path

import pandas as pd

_root = Path(__file__).parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from backtest_eth_krw import LOOKBACK_DAYS, MARKET as _DEFAULT_MARKET, load_prepared_market_data
from backtest_short_term_5m import ShortTermParams, build_short_term_context, run_short_term_backtest

MARKET = os.environ.get("MARKET", _DEFAULT_MARKET)

# ---------------------------------------------------------------------------
# 검증할 winner 파라미터 세트
# ---------------------------------------------------------------------------

ETH_WINNERS = [
    ("ETH 수익률 1위",  ShortTermParams(
        entry_kind="volatility_breakout",
        breakout_volume_mult=2.5, atr_breakout_mult=1.0,
        take_profit_pct=2.5, stop_loss_pct=1.5,
        max_hold_bars=36, trail_atr_mult=0.0,
        session_filter="kr_night", weekday_filter="weekday",
        atr_window=10, volume_baseline_window=30, breakout_lookback=40,
    )),
    ("ETH Sharpe 1위",  ShortTermParams(
        entry_kind="volatility_breakout",
        breakout_volume_mult=3.0, atr_breakout_mult=2.5,
        take_profit_pct=3.0, stop_loss_pct=1.5,
        max_hold_bars=36, trail_atr_mult=2.0,
        session_filter="kr_night", weekday_filter="weekday",
        atr_window=21, volume_baseline_window=30, breakout_lookback=10,
    )),
]

XRP_WINNERS = [
    # v3 확정 파라미터 (hold=60, lb=10, TP Ladder tp1=1.2/r=0.7)
    ("XRP 최종 winner [TP Ladder]",  ShortTermParams(
        entry_kind="volatility_breakout",
        breakout_volume_mult=1.5, atr_breakout_mult=1.5,
        take_profit_pct=2.0, stop_loss_pct=1.5,
        max_hold_bars=60, trail_atr_mult=0.0,
        session_filter="kr_night", weekday_filter="weekday",
        atr_window=14, volume_baseline_window=10, breakout_lookback=10,
        tp1_pct=1.2, tp1_ratio=0.7,
    )),
    ("XRP baseline [hold=60 tp1=OFF]",  ShortTermParams(
        entry_kind="volatility_breakout",
        breakout_volume_mult=1.5, atr_breakout_mult=1.5,
        take_profit_pct=2.0, stop_loss_pct=1.5,
        max_hold_bars=60, trail_atr_mult=0.0,
        session_filter="kr_night", weekday_filter="weekday",
        atr_window=14, volume_baseline_window=10, breakout_lookback=10,
        tp1_pct=0.0,
    )),
]

WINNERS = {"KRW-ETH": ETH_WINNERS, "KRW-XRP": XRP_WINNERS}


# ---------------------------------------------------------------------------
# 분할 & 검증
# ---------------------------------------------------------------------------

def run_validation(market: str) -> None:
    t0 = time.perf_counter()
    print(f"\n{'=' * 70}")
    print(f"  Train/Test 검증  —  {market}")
    print(f"{'=' * 70}\n")

    prepared_df = load_prepared_market_data(market=market, days=LOOKBACK_DAYS, use_cache=True)

    # 날짜 범위 확인
    dt_col = prepared_df["datetime_kst"]
    start_dt = dt_col.iloc[0]
    end_dt   = dt_col.iloc[-1]
    total_days = (end_dt - start_dt).days
    cutoff_days = int(total_days * 0.75)         # 앞 75% = train, 뒤 25% = test
    cutoff_dt = start_dt + pd.Timedelta(days=cutoff_days)

    train_df = prepared_df[dt_col < cutoff_dt].reset_index(drop=True)
    test_df  = prepared_df[dt_col >= cutoff_dt].reset_index(drop=True)

    print(f"전체 기간 : {start_dt.date()} ~ {end_dt.date()}  ({total_days}일, {len(prepared_df):,}캔들)")
    print(f"Train     : {start_dt.date()} ~ {cutoff_dt.date()}  ({cutoff_days}일, {len(train_df):,}캔들)")
    print(f"Test      : {cutoff_dt.date()} ~ {end_dt.date()}  ({total_days - cutoff_days}일, {len(test_df):,}캔들)\n")

    # buy&hold
    def bh(df):
        return (df["close"].iloc[-1] / df["open"].iloc[0] - 1.0) * 100.0

    print(f"Buy&Hold  — Train: {bh(train_df):.2f}%  /  Test: {bh(test_df):.2f}%\n")

    ctx_train = build_short_term_context(train_df)
    ctx_test  = build_short_term_context(test_df)

    winners = WINNERS.get(market, [])
    for label, params in winners:
        r_train = run_short_term_backtest(train_df, params, ctx_train)
        r_test  = run_short_term_backtest(test_df,  params, ctx_test)

        def fmt(r):
            return (f"return={r['return_pct']:+.2f}%  "
                    f"sharpe={r['sharpe_per_bar']:.3f}  "
                    f"MDD={r['max_drawdown_pct']:.2f}%  "
                    f"trips={r['completed_round_trips']}  "
                    f"win%={r['win_rate_pct']:.1f}")

        print(f"[ {label} ]")
        print(f"  Train : {fmt(r_train)}")
        print(f"  Test  : {fmt(r_test)}")

        # 일관성 판단
        consistent = (
            r_test["return_pct"] > 0
            and r_test["sharpe_per_bar"] > 0.5
            and r_test["completed_round_trips"] >= 5
        )
        verdict = "✓ 일관성 있음" if consistent else "✗ 주의 필요"
        print(f"  판정  : {verdict}\n")

    print(f"검증 완료 ({time.perf_counter() - t0:.1f}s)")


if __name__ == "__main__":
    markets = [MARKET] if MARKET != "ALL" else ["KRW-ETH", "KRW-XRP"]
    for m in markets:
        run_validation(m)
