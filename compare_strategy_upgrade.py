"""
현재 라이브 전략과 투자기회 확대 후보를 1년 5분봉 캐시로 비교한다.

목표:
  - 기존 수익률을 훼손하지 않는 범위에서 completed_round_trips를 늘리는 후보 확인
  - 라이브 main_multi_market.py 변경 전후의 수익률, MDD, Sharpe, PF를 같은 엔진으로 검증
"""

import sys
from pathlib import Path

_root = Path(__file__).parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from backtest_eth_krw import LOOKBACK_DAYS, load_prepared_market_data
from backtest_short_term_5m import (
    ShortTermParams,
    build_short_term_context,
    run_short_term_backtest,
)


STRATEGIES: dict[str, dict[str, dict]] = {
    "KRW-XRP": {
        "current": {
            "breakout_volume_mult": 1.5,
            "atr_breakout_mult": 1.5,
            "volume_baseline_window": 10,
            "breakout_lookback": 10,
            "max_hold_bars": 60,
            "take_profit_pct": 2.0,
            "stop_loss_pct": 1.5,
            "tp1_pct": 1.2,
            "tp1_ratio": 0.7,
            "session_filter": "kr_night",
            "weekday_filter": "weekday",
        },
    },
    "KRW-ETH": {
        "current": {
            "breakout_volume_mult": 2.5,
            "atr_breakout_mult": 2.0,
            "volume_baseline_window": 20,
            "breakout_lookback": 10,
            "max_hold_bars": 24,
            "take_profit_pct": 2.5,
            "stop_loss_pct": 1.0,
            "tp1_pct": 0.0,
            "tp1_ratio": 0.5,
            "session_filter": "kr_night",
            "weekday_filter": "weekday",
        },
        "proposed": {
            "breakout_volume_mult": 2.5,
            "atr_breakout_mult": 1.0,
            "volume_baseline_window": 10,
            "breakout_lookback": 10,
            "max_hold_bars": 60,
            "take_profit_pct": 2.0,
            "stop_loss_pct": 1.5,
            "tp1_pct": 0.0,
            "tp1_ratio": 0.5,
            "session_filter": "kr_night",
            "weekday_filter": "all",
        },
    },
    "KRW-BTC": {
        "current_code": {
            "breakout_volume_mult": 2.5,
            "atr_breakout_mult": 2.0,
            "volume_baseline_window": 10,
            "breakout_lookback": 10,
            "max_hold_bars": 48,
            "take_profit_pct": 3.0,
            "stop_loss_pct": 2.0,
            "tp1_pct": 0.0,
            "tp1_ratio": 0.5,
            "session_filter": "all",
            "weekday_filter": "all",
        },
        "proposed": {
            "breakout_volume_mult": 2.5,
            "atr_breakout_mult": 2.0,
            "volume_baseline_window": 10,
            "breakout_lookback": 10,
            "max_hold_bars": 48,
            "take_profit_pct": 3.0,
            "stop_loss_pct": 2.0,
            "tp1_pct": 0.0,
            "tp1_ratio": 0.5,
            "session_filter": "kr_day",
            "weekday_filter": "all",
        },
    },
}


def _params(values: dict) -> ShortTermParams:
    return ShortTermParams(
        entry_kind="volatility_breakout",
        atr_window=14,
        trail_atr_mult=0.0,
        **values,
    )


def _fmt(result: dict) -> str:
    return (
        f"return={result['return_pct']:+7.2f}%  "
        f"trips={result['completed_round_trips']:>4}  "
        f"MDD={result['max_drawdown_pct']:+7.2f}%  "
        f"Sharpe={result['sharpe_per_bar']:>6.3f}  "
        f"PF={result['profit_factor']:>5.2f}  "
        f"win={result['win_rate_pct']:>5.1f}%"
    )


def main() -> None:
    for market, variants in STRATEGIES.items():
        df = load_prepared_market_data(market=market, days=LOOKBACK_DAYS, use_cache=True)
        context = build_short_term_context(df)
        print(f"\n{market}  {df['datetime_kst'].iloc[0].date()} ~ {df['datetime_kst'].iloc[-1].date()}")
        for label, values in variants.items():
            result = run_short_term_backtest(df, _params(values), context)
            print(f"  {label:<12} {_fmt(result)}")


if __name__ == "__main__":
    main()
