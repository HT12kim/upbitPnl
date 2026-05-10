"""
시장별(ETH/BTC) 그리드 winner 후보 train/test 검증.

각 시장에서 5종 후보 선정 → train(75%)/test(25%) 분할 → 일관성 확인.
"""

import sys
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


def select_candidates(df: pd.DataFrame) -> list[tuple[str, dict]]:
    """양수 + trips≥30 + MDD≥-15% 통과 풀에서 5개 후보 선정."""
    pos = df[(df["return_pct"] > 0) & (df["completed_round_trips"] >= 30) &
             (df["max_drawdown_pct"] >= -15.0)].copy()
    if len(pos) == 0:
        return []
    cands: list[tuple[str, dict]] = []
    seen_keys = set()

    def _key(row):
        return tuple(row[c] for c in [
            "session_filter", "weekday_filter", "breakout_volume_mult", "atr_breakout_mult",
            "volume_baseline_window", "breakout_lookback", "max_hold_bars",
            "take_profit_pct", "stop_loss_pct", "tp1_pct", "tp1_ratio",
        ])

    def _add(label, row):
        k = _key(row)
        if k in seen_keys:
            return
        seen_keys.add(k)
        cands.append((label, row.to_dict()))

    _add("Sharpe Top",    pos.sort_values("sharpe_per_bar", ascending=False).iloc[0])
    _add("Return Top",    pos.sort_values("return_pct", ascending=False).iloc[0])
    _add("Trips Top",     pos.sort_values("completed_round_trips", ascending=False).iloc[0])
    sharpe_ge_1 = pos[pos["sharpe_per_bar"] >= 1.0]
    if len(sharpe_ge_1):
        _add("Best MDD (Sh≥1)", sharpe_ge_1.sort_values("max_drawdown_pct", ascending=False).iloc[0])
    _add("PF Top",        pos.sort_values("profit_factor", ascending=False).iloc[0])

    return cands


def to_params(row: dict) -> ShortTermParams:
    return ShortTermParams(
        entry_kind="volatility_breakout",
        breakout_volume_mult=float(row["breakout_volume_mult"]),
        atr_breakout_mult=float(row["atr_breakout_mult"]),
        take_profit_pct=float(row["take_profit_pct"]),
        stop_loss_pct=float(row["stop_loss_pct"]),
        max_hold_bars=int(row["max_hold_bars"]),
        trail_atr_mult=0.0,
        session_filter=row["session_filter"],
        weekday_filter=row["weekday_filter"],
        atr_window=int(row["atr_window"]),
        volume_baseline_window=int(row["volume_baseline_window"]),
        breakout_lookback=int(row["breakout_lookback"]),
        tp1_pct=float(row["tp1_pct"]),
        tp1_ratio=float(row["tp1_ratio"]) if float(row["tp1_pct"]) > 0 else 0.5,
    )


def fmt(r: dict) -> str:
    return (f"return={r['return_pct']:+7.2f}%  Sharpe={r['sharpe_per_bar']:6.3f}  "
            f"MDD={r['max_drawdown_pct']:+7.2f}%  trips={r['completed_round_trips']:>3}  "
            f"win={r['win_rate_pct']:5.1f}%  PF={r['profit_factor']:5.2f}")


def validate_market(market: str, csv_path: Path) -> None:
    print(f"\n{'=' * 105}")
    print(f"  Train(75%) / Test(25%) 검증  —  {market}")
    print(f"{'=' * 105}")

    df = pd.read_csv(csv_path)
    cands = select_candidates(df)
    if not cands:
        print("  1차필터 통과 후보 없음")
        return

    # 데이터 분할
    full = load_prepared_market_data(market=market, days=LOOKBACK_DAYS, use_cache=True)
    dt_col = full["datetime_kst"]
    start = dt_col.iloc[0]
    end = dt_col.iloc[-1]
    total_days = (end - start).days
    cutoff_days = int(total_days * 0.75)
    cutoff = start + pd.Timedelta(days=cutoff_days)

    train_df = full[dt_col < cutoff].reset_index(drop=True)
    test_df  = full[dt_col >= cutoff].reset_index(drop=True)

    def bh(d):
        return (float(d["close"].iloc[-1]) / float(d["open"].iloc[0]) - 1.0) * 100.0

    print(f"전체  : {start.date()} ~ {end.date()}  ({total_days}일, {len(full):,}캔들)")
    print(f"Train : {start.date()} ~ {cutoff.date()}  ({cutoff_days}일, {len(train_df):,}캔들)  BnH={bh(train_df):+6.2f}%")
    print(f"Test  : {cutoff.date()} ~ {end.date()}    ({total_days - cutoff_days}일, {len(test_df):,}캔들)  BnH={bh(test_df):+6.2f}%\n")

    ctx_train = build_short_term_context(train_df)
    ctx_test  = build_short_term_context(test_df)

    for label, row in cands:
        params = to_params(row)
        r_tr = run_short_term_backtest(train_df, params, ctx_train)
        r_te = run_short_term_backtest(test_df,  params, ctx_test)

        consistent = (
            r_tr["return_pct"] > 0 and r_te["return_pct"] > 0
            and r_te["sharpe_per_bar"] >= 0.5
            and r_te["completed_round_trips"] >= 5
            and r_tr["profit_factor"] >= 1.0 and r_te["profit_factor"] >= 1.0
        )
        verdict = "✓ 일관성 있음" if consistent else "✗ 주의"

        print(f"[{label}]  {verdict}")
        print(f"  파라미터: vol={row['breakout_volume_mult']} atr={row['atr_breakout_mult']} "
              f"vbase={int(row['volume_baseline_window'])} lb={int(row['breakout_lookback'])} "
              f"hold={int(row['max_hold_bars'])} tp={row['take_profit_pct']} sl={row['stop_loss_pct']} "
              f"tp1={row['tp1_pct']}/{row['tp1_ratio']} ses={row['session_filter']}/{row['weekday_filter']}")
        print(f"  Train : {fmt(r_tr)}")
        print(f"  Test  : {fmt(r_te)}\n")


if __name__ == "__main__":
    for market in ["KRW-ETH", "KRW-BTC"]:
        coin = market.replace("KRW-", "").lower()
        csv = Path(f"data_cache/{coin}_per_market_grid_1y.csv")
        if not csv.exists():
            print(f"⚠️  {csv} 없음 — 그리드 먼저 실행 필요")
            continue
        validate_market(market, csv)
