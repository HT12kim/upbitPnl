from itertools import product
from pathlib import Path

import pandas as pd

from backtest_eth_krw import LOOKBACK_DAYS, MARKET, StrategyParams, build_backtest_context, load_prepared_market_data, run_backtest


STOP_LOSS_VALUES = [0.5, 0.7, 1.0, 1.5, 2.0]
ENTRY_EMA_MODES = ["all", "fast", "ema5_only"]
EXIT_EMA_MODES = ["5_below_10", "5_below_20", "10_below_20"]
REBOUND_VOLUME_MULTIPLIERS = [0.0, 1.0, 1.2]
BREAKOUT_VOLUME_MULTIPLIERS = [1.0, 1.2, 1.5]
OUTPUT_PATH = Path("data_cache/eth_strategy_optimization.csv")


def optimize() -> pd.DataFrame:
    prepared_df = load_prepared_market_data(market=MARKET, days=LOOKBACK_DAYS, use_cache=True)
    context = build_backtest_context(prepared_df)
    results: list[dict] = []

    for stop_loss_pct, entry_ema_mode, exit_ema_mode, rebound_volume_multiplier, breakout_volume_multiplier in product(
        STOP_LOSS_VALUES,
        ENTRY_EMA_MODES,
        EXIT_EMA_MODES,
        REBOUND_VOLUME_MULTIPLIERS,
        BREAKOUT_VOLUME_MULTIPLIERS,
    ):
        params = StrategyParams(
            stop_loss_pct=stop_loss_pct,
            entry_ema_mode=entry_ema_mode,
            exit_ema_mode=exit_ema_mode,
            rebound_volume_multiplier=rebound_volume_multiplier,
            breakout_volume_multiplier=breakout_volume_multiplier,
        )
        result = run_backtest(prepared_df=prepared_df, params=params, context=context)
        results.append(
            {
                "stop_loss_pct": stop_loss_pct,
                "entry_ema_mode": entry_ema_mode,
                "exit_ema_mode": exit_ema_mode,
                "rebound_volume_multiplier": rebound_volume_multiplier,
                "breakout_volume_multiplier": breakout_volume_multiplier,
                "return_pct": result["return_pct"],
                "final_equity": result["final_equity"],
                "max_drawdown_pct": result["max_drawdown_pct"],
                "completed_round_trips": result["completed_round_trips"],
                "win_rate_pct": result["win_rate_pct"],
            }
        )

    result_df = pd.DataFrame(results)
    result_df.sort_values(
        by=["return_pct", "max_drawdown_pct", "completed_round_trips"],
        ascending=[False, False, True],
        inplace=True,
    )
    result_df.reset_index(drop=True, inplace=True)

    return result_df


if __name__ == "__main__":
    optimized_df = optimize()
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    optimized_df.to_csv(OUTPUT_PATH, index=False)
    print("top_10_by_return:")
    print(optimized_df.head(10).to_string(index=False))
    print("")
    print("top_10_low_drawdown_among_positive:")
    positive_df = optimized_df[optimized_df["return_pct"] > 0].copy()
    if positive_df.empty:
        print("no_positive_result")
    else:
        positive_df.sort_values(
            by=["max_drawdown_pct", "return_pct", "completed_round_trips"],
            ascending=[False, False, True],
            inplace=True,
        )
        print(positive_df.head(10).to_string(index=False))
