"""
그리드 CSV에서 실거래 반영 후보를 선별해 strategy_winners 설정을 생성한다.

기본 출력:
  strategy_winners.generated.json

실거래 설정 파일까지 교체:
  APPLY=1 python3 select_strategy_winners.py
"""

from __future__ import annotations

import json
import os
import sys
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

MARKETS = os.environ.get("MARKETS", "KRW-XRP,KRW-ETH,KRW-BTC").split(",")
WINDOW = os.environ.get("WINDOW", "1y")
MIN_TRIPS = int(os.environ.get("MIN_TRIPS", "30"))
MIN_TEST_TRIPS = int(os.environ.get("MIN_TEST_TRIPS", "5"))
MAX_MDD = float(os.environ.get("MAX_MDD", "-15.0"))
APPLY = os.environ.get("APPLY", "0") == "1"

BASE_PATH = Path("strategy_winners.json")
GENERATED_PATH = Path("strategy_winners.generated.json")


def _load_base_payload() -> dict[str, Any]:
    if BASE_PATH.exists():
        return json.loads(BASE_PATH.read_text(encoding="utf-8"))
    return {"description": "generated strategy winners", "market_configs": {}}


def _to_params(row: pd.Series) -> ShortTermParams:
    return ShortTermParams(
        entry_kind=str(row["entry_kind"]),
        breakout_volume_mult=float(row["breakout_volume_mult"]),
        atr_breakout_mult=float(row["atr_breakout_mult"]),
        take_profit_pct=float(row["take_profit_pct"]),
        stop_loss_pct=float(row["stop_loss_pct"]),
        max_hold_bars=int(row["max_hold_bars"]),
        trail_atr_mult=0.0,
        session_filter=str(row["session_filter"]),
        weekday_filter=str(row["weekday_filter"]),
        atr_window=int(row.get("atr_window", 14)),
        volume_baseline_window=int(row["volume_baseline_window"]),
        breakout_lookback=int(row["breakout_lookback"]),
        tp1_pct=float(row.get("tp1_pct", 0.0)),
        tp1_ratio=float(row.get("tp1_ratio", 0.5)) if float(row.get("tp1_pct", 0.0)) > 0 else 0.5,
        vwap_volume_mult=float(row["breakout_volume_mult"]),
    )


def _score_candidates(df: pd.DataFrame) -> pd.DataFrame:
    filtered = df[
        (df["return_pct"] > 0)
        & (df["completed_round_trips"] >= MIN_TRIPS)
        & (df["max_drawdown_pct"] >= MAX_MDD)
        & (df["profit_factor"] >= 1.0)
    ].copy()
    if len(filtered) == 0:
        return filtered

    # 과최적화 방지를 위해 수익률만 보지 않고, MDD와 거래 수를 같이 반영한다.
    filtered["score"] = (
        filtered["return_pct"]
        + filtered["sharpe_per_bar"] * 2.0
        + filtered["completed_round_trips"] * 0.02
        + filtered["max_drawdown_pct"] * 1.5
    )
    return filtered.sort_values("score", ascending=False)


def _validate_train_test(market: str, row: pd.Series) -> tuple[bool, dict[str, Any]]:
    full = load_prepared_market_data(market=market, days=LOOKBACK_DAYS, use_cache=True)
    dt = full["datetime_kst"]
    cutoff = dt.iloc[0] + (dt.iloc[-1] - dt.iloc[0]) * 0.75
    train_df = full[dt < cutoff].reset_index(drop=True)
    test_df = full[dt >= cutoff].reset_index(drop=True)

    params = _to_params(row)
    train_result = run_short_term_backtest(train_df, params, build_short_term_context(train_df))
    test_result = run_short_term_backtest(test_df, params, build_short_term_context(test_df))
    passed = (
        train_result["return_pct"] > 0
        and test_result["return_pct"] > 0
        and test_result["completed_round_trips"] >= MIN_TEST_TRIPS
        and train_result["profit_factor"] >= 1.0
        and test_result["profit_factor"] >= 1.0
    )
    return passed, {"train": train_result, "test": test_result}


def _winner_config(market: str, row: pd.Series, validation: dict[str, Any]) -> dict[str, Any]:
    asset = market.replace("KRW-", "")
    return {
        "label": f"{asset} {row['entry_kind']} winner",
        "entry_kind": str(row["entry_kind"]),
        "breakout_volume_mult": float(row["breakout_volume_mult"]),
        "atr_breakout_mult": float(row["atr_breakout_mult"]),
        "atr_window": int(row.get("atr_window", 14)),
        "volume_baseline_window": int(row["volume_baseline_window"]),
        "breakout_lookback": int(row["breakout_lookback"]),
        "take_profit": float(row["take_profit_pct"]),
        "stop_loss": float(row["stop_loss_pct"]),
        "max_hold_bars": int(row["max_hold_bars"]),
        "tp1_pct": float(row.get("tp1_pct", 0.0)),
        "tp1_ratio": float(row.get("tp1_ratio", 0.5)),
        "session": str(row["session_filter"]),
        "weekday": str(row["weekday_filter"]),
        "selection_metrics": {
            "window": WINDOW,
            "return_pct": float(row["return_pct"]),
            "sharpe_per_bar": float(row["sharpe_per_bar"]),
            "max_drawdown_pct": float(row["max_drawdown_pct"]),
            "profit_factor": float(row["profit_factor"]),
            "completed_round_trips": int(row["completed_round_trips"]),
            "train_return_pct": validation["train"]["return_pct"],
            "test_return_pct": validation["test"]["return_pct"],
            "test_round_trips": validation["test"]["completed_round_trips"],
        },
    }


def select_winners() -> dict[str, Any]:
    payload = _load_base_payload()
    payload.setdefault("market_configs", {})

    for raw_market in MARKETS:
        market = raw_market.strip()
        if not market:
            continue
        coin = market.replace("KRW-", "").lower()
        csv_path = Path(f"data_cache/{coin}_multi_strategy_grid_{WINDOW}.csv")
        if not csv_path.exists():
            print(f"[{market}] 스킵: {csv_path} 없음")
            continue

        ranked = _score_candidates(pd.read_csv(csv_path))
        if len(ranked) == 0:
            print(f"[{market}] 스킵: 필터 통과 후보 없음")
            continue

        selected = None
        selected_validation = None
        for _, row in ranked.head(20).iterrows():
            passed, validation = _validate_train_test(market, row)
            if passed:
                selected = row
                selected_validation = validation
                break

        if selected is None or selected_validation is None:
            print(f"[{market}] 스킵: train/test 통과 후보 없음")
            continue

        payload["market_configs"][market] = _winner_config(market, selected, selected_validation)
        print(
            f"[{market}] 선택: {selected['entry_kind']} "
            f"return={selected['return_pct']:+.2f}% "
            f"trips={int(selected['completed_round_trips'])} "
            f"MDD={selected['max_drawdown_pct']:+.2f}%"
        )

    return payload


if __name__ == "__main__":
    output = select_winners()
    target = BASE_PATH if APPLY else GENERATED_PATH
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"설정 저장: {target}")
