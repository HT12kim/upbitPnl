"""
5분봉 초단기 ETH 매매 전략 백테스트

진입 시그널 4종: trend_pullback | mean_reversion | volatility_breakout | combo_or
청산 규칙: 손절 / 익절 / 트레일링 / 최대 보유봉 (공통)
체결 방식: T+1 open (시그널 캔들 종가 기준 판단 → 다음 캔들 open 시장가 체결)
"""

import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

# 프로젝트 루트를 sys.path에 추가 (스크립트 직접 실행 시)
_root = Path(__file__).parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from backtest_eth_krw import (
    INITIAL_CASH,
    FEE_RATE,
    LOOKBACK_DAYS,
    MARKET,
    MIN_ORDER_KRW,
    calculate_max_drawdown,
    load_prepared_market_data,
)
from trading.trading_strategy2 import _calculate_bollinger, _calculate_rsi

# ---------------------------------------------------------------------------
# 파라미터 정의
# ---------------------------------------------------------------------------

# 유효한 EMA 쌍 (fast_span, slow_span)
VALID_EMA_PAIRS = [(5, 20), (8, 21), (10, 30)]
# 유효한 ATR 돌파 배수 (기존 + v2 확장)
VALID_ATR_MULT   = [0.5, 1.0, 1.5]
VALID_ATR_MULTS_V2 = [0.5, 1.0, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 2.0, 2.5]
# 새 파라미터 유효값 (zoom-in 탐색 범위)
VALID_ATR_WINDOWS        = [10, 14, 21]
VALID_VOL_BASELINES      = [10, 20, 30, 50]
VALID_BREAKOUT_LOOKBACKS = [10, 20, 40]


@dataclass(frozen=True)
class ShortTermParams:
    entry_kind: str = "mean_reversion"
    # 추세 추종 (trend_pullback) 파라미터
    ema_fast: int = 5
    ema_slow: int = 20
    slope_lookback: int = 1       # EMA 기울기 측정 캔들 수 (1 or 3)
    # 평균 회귀 (mean_reversion) 파라미터
    rsi_buy_threshold: int = 30   # RSI 진입 기준선 (25, 30, 35)
    bb_std: float = 2.0           # 볼린저밴드 표준편차 (2.0, 2.5)
    # 변동성 돌파 (volatility_breakout) 파라미터
    breakout_volume_mult: float = 1.5  # 거래량 배수 (volume / Volume_MA20)
    atr_breakout_mult: float = 1.0     # ATR 돌파 배수 (0.5, 1.0, 1.5)
    # 공통 청산 파라미터
    take_profit_pct: float = 0.8
    stop_loss_pct: float = 0.8
    max_hold_bars: int = 6        # 강제청산 캔들 수 (3=15분, 6=30분, 12=1시간, 24=2시간)
    trail_atr_mult: float = 0.0   # 트레일링 stop ATR 배수 (0 = 비활성)
    # 시간대 / 요일 필터
    session_filter: str = "all"   # all | kr_day (09-18 KST) | us_open (22-02 KST) | kr_night (00-08 KST)
    weekday_filter: str = "all"   # all | weekday (월-금) | weekend (토-일)
    # Grid zoom-in 파라미터 (v2)
    atr_window: int = 14              # ATR 계산 기간 (10, 14, 21)
    volume_baseline_window: int = 20  # 거래량 MA 기간 (10, 20, 30, 50)
    breakout_lookback: int = 20       # 직전 N캔들 최고가 lookback (10, 20, 40)
    # Partial TP Ladder (v3)
    tp1_pct: float = 0.0    # 1차 부분 익절 수준 % (0 = 비활성)
    tp1_ratio: float = 0.5  # 1차 익절 시 매도할 포지션 비율 (0.5 = 50%)


# ---------------------------------------------------------------------------
# 인디케이터 추가 (ATR14)
# ---------------------------------------------------------------------------

def prepare_short_term_indicators(prepared_df: pd.DataFrame) -> pd.DataFrame:
    """backtest_eth_krw.prepare_indicators 결과에 ATR14를 추가로 계산한다."""
    df = prepared_df.copy()
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["ATR14"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    return df


# ---------------------------------------------------------------------------
# Context 빌드 (한 번만 실행, 모든 조합에서 재사용)
# ---------------------------------------------------------------------------

def build_short_term_context(prepared_df: pd.DataFrame) -> dict[str, Any]:
    """
    모든 시그널 판단에 필요한 배열을 사전 계산한다.
    반환된 dict는 run_short_term_backtest()에서 재사용된다.
    """
    df = prepare_short_term_indicators(prepared_df)
    n = len(df)
    close = df["close"]

    # ── 시간대 / 요일 마스크 ────────────────────────────────────────────────
    dt = df["datetime_kst"]
    hour = dt.dt.hour
    weekday = dt.dt.weekday  # 0=월, 6=일
    kr_day_mask    = ((hour >= 9) & (hour < 18)).to_numpy(dtype=bool)
    us_open_mask   = ((hour >= 22) | (hour < 2)).to_numpy(dtype=bool)
    kr_night_mask  = ((hour >= 0) & (hour < 9)).to_numpy(dtype=bool)
    weekday_mask   = (weekday < 5).to_numpy(dtype=bool)
    weekend_mask   = (weekday >= 5).to_numpy(dtype=bool)

    # ── 기본 가격·거래량 배열 ──────────────────────────────────────────────
    open_arr    = df["open"].to_numpy(dtype=float)
    close_arr   = close.to_numpy(dtype=float)
    high_arr    = df["high"].to_numpy(dtype=float)
    low_arr     = df["low"].to_numpy(dtype=float)
    is_bullish  = (close > df["open"]).to_numpy(dtype=bool)
    vol_ratio   = (df["volume"] / df["Volume_MA20"]).replace([math.inf, -math.inf], 0).fillna(0).to_numpy(dtype=float)
    atr14       = df["ATR14"].to_numpy(dtype=float)

    # ── ATR 돌파 임계값 (직전 20캔들 최고가 + ATR × k) ─────────────────────
    rolling_high_prev = df["high"].shift(1).rolling(20, min_periods=1).max()
    bt_arrays: dict[float, np.ndarray] = {}
    for mult in VALID_ATR_MULT:
        bt_arrays[mult] = (rolling_high_prev + df["ATR14"] * mult).to_numpy(dtype=float)

    # ── ATR 다중 윈도우 배열 (zoom-in) ─────────────────────────────────────
    def _compute_atr_w(window: int) -> np.ndarray:
        _tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"]  - df["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        return _tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean().to_numpy(dtype=float)

    atr_arrays: dict[int, np.ndarray] = {w: _compute_atr_w(w) for w in VALID_ATR_WINDOWS}

    # ── 거래량 비율 다중 기준선 배열 (zoom-in) ──────────────────────────────
    vol_ratio_arrays: dict[int, np.ndarray] = {}
    for _baseline in VALID_VOL_BASELINES:
        _vol_ma = df["volume"].rolling(_baseline, min_periods=1).mean()
        vol_ratio_arrays[_baseline] = (
            (df["volume"] / _vol_ma).replace([math.inf, -math.inf], 0).fillna(0).to_numpy(dtype=float)
        )

    # ── ATR 돌파 임계값 v2 (atr_window × atr_mult × breakout_lookback) ─────
    _rolling_highs: dict[int, pd.Series] = {
        lb: df["high"].shift(1).rolling(lb, min_periods=1).max()
        for lb in VALID_BREAKOUT_LOOKBACKS
    }
    bt_arrays_v2: dict[tuple, np.ndarray] = {}
    for _w in VALID_ATR_WINDOWS:
        _atr_s = pd.Series(atr_arrays[_w], index=df.index)
        for _mult in VALID_ATR_MULTS_V2:
            for _lb in VALID_BREAKOUT_LOOKBACKS:
                bt_arrays_v2[(_w, _mult, _lb)] = (_rolling_highs[_lb] + _atr_s * _mult).to_numpy(dtype=float)

    # ── EMA 쌍별 시그널 배열 ──────────────────────────────────────────────
    ema_pair_data: dict[tuple[int, int], dict[str, np.ndarray]] = {}
    for (fast_span, slow_span) in VALID_EMA_PAIRS:
        ema_fast = close.ewm(span=fast_span, adjust=False).mean()
        ema_slow = close.ewm(span=slow_span, adjust=False).mean()
        # 기울기: 1봉, 3봉
        slope_lb1 = ema_fast.diff().to_numpy(dtype=float)
        slope_lb3 = ema_fast.diff(3).to_numpy(dtype=float)
        # 직전 5캔들 중 close < EMA_fast 구간 존재 여부 (pullback 확인)
        below_fast = (close.shift(1) < ema_fast.shift(1))
        pullback_5  = below_fast.rolling(5, min_periods=1).max().fillna(0).astype(bool).to_numpy()
        ema_pair_data[(fast_span, slow_span)] = {
            "ema_fast":       ema_fast.to_numpy(dtype=float),
            "ema_slow":       ema_slow.to_numpy(dtype=float),
            "fast_above_slow": (ema_fast > ema_slow).fillna(False).to_numpy(dtype=bool),
            "slope_lb1":      slope_lb1,
            "slope_lb3":      slope_lb3,
            "pullback_5":     pullback_5,
        }

    # ── 평균 회귀 시그널 배열 (RSI / BB) ──────────────────────────────────
    rsi14 = _calculate_rsi(close, window=14)

    def _rsi_recent_under(thr: int, win: int = 5) -> np.ndarray:
        return (rsi14 < thr).rolling(win, min_periods=1).max().fillna(False).astype(bool).to_numpy()

    def _prev_below_bb(std_val: float) -> np.ndarray:
        _, _, bb_lower = _calculate_bollinger(close, window=20, num_std=std_val)
        return (close.shift(1) < bb_lower).fillna(False).to_numpy(dtype=bool)

    # ── signal_time / next_signal_time ────────────────────────────────────
    sig_time      = (df["date"] + " " + df["time"]).to_numpy()
    next_sig_time = (
        df["date"].shift(-1).fillna(df["date"]) + " " +
        df["time"].shift(-1).fillna(df["time"])
    ).to_numpy()

    return {
        # 기본
        "open":           open_arr,
        "close":          close_arr,
        "high":           high_arr,
        "low":            low_arr,
        "is_bullish":     is_bullish,
        "volume_ratio":   vol_ratio,
        "atr14":          atr14,
        "signal_time":    sig_time,
        "next_signal_time": next_sig_time,
        "datetime_kst":   dt,
        "n":              n,
        # 시간대
        "kr_day_mask":    kr_day_mask,
        "us_open_mask":   us_open_mask,
        "kr_night_mask":  kr_night_mask,
        "weekday_mask":   weekday_mask,
        "weekend_mask":   weekend_mask,
        # ATR 돌파 임계값
        "bt_arrays":      bt_arrays,
        # zoom-in v2 배열
        "atr_arrays":       atr_arrays,
        "vol_ratio_arrays": vol_ratio_arrays,
        "bt_arrays_v2":     bt_arrays_v2,
        # EMA 쌍
        "ema_pairs":      ema_pair_data,
        # 평균 회귀
        "rsi_recent_under_25": _rsi_recent_under(25),
        "rsi_recent_under_30": _rsi_recent_under(30),
        "rsi_recent_under_35": _rsi_recent_under(35),
        "prev_below_bb_20":    _prev_below_bb(2.0),
        "prev_below_bb_25":    _prev_below_bb(2.5),
        # buy_and_hold 기준값 (검증용)
        "first_open":     float(open_arr[0]),
        "last_close":     float(close_arr[-1]),
    }


# ---------------------------------------------------------------------------
# 백테스트 실행
# ---------------------------------------------------------------------------

def run_short_term_backtest(
    prepared_df: pd.DataFrame,
    params: ShortTermParams,
    context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if context is None:
        context = build_short_term_context(prepared_df)

    n = context["n"]
    close = context["close"]
    open_prices = context["open"]
    atr14 = context["atr_arrays"][params.atr_window]

    # ── 시간대 / 요일 필터 (사전 계산된 bool 배열 선택) ──────────────────
    session_map = {
        "all":      np.ones(n, dtype=bool),
        "kr_day":   context["kr_day_mask"],
        "us_open":  context["us_open_mask"],
        "kr_night": context["kr_night_mask"],
    }
    weekday_map = {
        "all":     np.ones(n, dtype=bool),
        "weekday": context["weekday_mask"],
        "weekend": context["weekend_mask"],
    }
    time_filter = session_map[params.session_filter] & weekday_map[params.weekday_filter]

    # ── 진입 시그널 배열 (hot loop 전에 벡터 연산으로 계산) ────────────────
    ema_pair = context["ema_pairs"][(params.ema_fast, params.ema_slow)]
    slope_arr = ema_pair["slope_lb1"] if params.slope_lookback == 1 else ema_pair["slope_lb3"]

    trend_signal = (
        ema_pair["fast_above_slow"]
        & ema_pair["pullback_5"]
        & (close > ema_pair["ema_fast"])
        & (slope_arr > 0)
    ) & time_filter

    rsi_key = f"rsi_recent_under_{params.rsi_buy_threshold}"
    bb_key  = f"prev_below_bb_{int(params.bb_std * 10)}"
    mean_rev_signal = (
        context[bb_key]
        & context["is_bullish"]
        & context[rsi_key]
    ) & time_filter

    bt = context["bt_arrays_v2"][(params.atr_window, params.atr_breakout_mult, params.breakout_lookback)]
    vol_signal = (
        (close > bt)
        & (context["vol_ratio_arrays"][params.volume_baseline_window] >= params.breakout_volume_mult)
    ) & time_filter

    kind = params.entry_kind
    if kind == "trend_pullback":
        entry_signal = trend_signal
    elif kind == "mean_reversion":
        entry_signal = mean_rev_signal
    elif kind == "volatility_breakout":
        entry_signal = vol_signal
    elif kind == "combo_or":
        entry_signal = trend_signal | mean_rev_signal | vol_signal
    else:
        raise ValueError(f"지원하지 않는 entry_kind: {kind}")

    # NaN 대응: False 처리
    entry_signal = np.where(np.isnan(entry_signal.astype(float)), False, entry_signal).astype(bool)

    # ── 시뮬레이션 루프 ───────────────────────────────────────────────────
    cash: float = float(INITIAL_CASH)
    asset_qty: float = 0.0
    buy_price: Optional[float] = None
    buy_cost_krw: float = 0.0
    buy_index: Optional[int] = None
    peak_close: float = 0.0
    tp1_taken: bool = False        # 현재 포지션에서 TP1 실행 여부
    current_trip_pnl: float = 0.0  # 현재 round-trip 누적 PnL (TP1 포함)

    trades: list[dict] = []
    equity_curve: list[float] = []
    hold_bars_list: list[int] = []

    sig_times  = context["signal_time"]
    next_times = context["next_signal_time"]

    tp1_pct_val   = params.tp1_pct
    tp1_ratio_val = params.tp1_ratio

    for i in range(200, n - 1):
        cur_close   = close[i]
        exec_price  = open_prices[i + 1]
        sell_signal = False
        sell_reason = ""

        if asset_qty > 0 and buy_price is not None and buy_index is not None:
            if cur_close > peak_close:
                peak_close = cur_close
            # 1. 손절
            if cur_close <= buy_price * (1.0 - params.stop_loss_pct / 100.0):
                sell_signal = True
                sell_reason = "stop_loss"
            # 2. 익절
            elif cur_close >= buy_price * (1.0 + params.take_profit_pct / 100.0):
                sell_signal = True
                sell_reason = "take_profit"
            else:
                # 3. 트레일링 stop
                if params.trail_atr_mult > 0:
                    trail_thr = peak_close - atr14[i] * params.trail_atr_mult
                    if cur_close <= trail_thr:
                        sell_signal = True
                        sell_reason = "trailing"
                # 4. 최대 보유봉 초과
                if not sell_signal and (i - buy_index) >= params.max_hold_bars:
                    sell_signal = True
                    sell_reason = "max_hold"

            # 5. 1차 부분 익절 (전체 청산이 없을 때만)
            if (not sell_signal
                    and tp1_pct_val > 0
                    and not tp1_taken
                    and cur_close >= buy_price * (1.0 + tp1_pct_val / 100.0)):
                tp1_qty     = tp1_ratio_val * asset_qty
                tp1_cost    = tp1_ratio_val * buy_cost_krw
                tp1_proceeds = tp1_qty * exec_price * (1.0 - FEE_RATE)
                tp1_pnl_part = tp1_proceeds - tp1_cost
                cash          += tp1_proceeds
                asset_qty     -= tp1_qty
                buy_cost_krw  -= tp1_cost
                tp1_taken      = True
                current_trip_pnl += tp1_pnl_part
                trades.append({
                    "side":           "sell",
                    "signal_time":    sig_times[i],
                    "execution_time": next_times[i],
                    "price":          exec_price,
                    "pnl":            tp1_pnl_part,
                    "hold_bars":      i - buy_index,
                    "reason":         "partial_tp1",
                })

        if asset_qty == 0 and entry_signal[i]:
            order_krw = math.floor(cash * 0.999)
            if order_krw >= MIN_ORDER_KRW:
                asset_qty        = (order_krw * (1.0 - FEE_RATE)) / exec_price
                cash            -= order_krw
                buy_price        = exec_price
                buy_cost_krw     = float(order_krw)
                buy_index        = i
                peak_close       = exec_price
                tp1_taken        = False
                current_trip_pnl = 0.0
                trades.append({
                    "side":           "buy",
                    "signal_time":    sig_times[i],
                    "execution_time": next_times[i],
                    "price":          exec_price,
                    "pnl":            0.0,
                    "hold_bars":      0,
                    "reason":         "",
                })
        elif asset_qty > 0 and sell_signal:
            sell_krw  = asset_qty * exec_price * (1.0 - FEE_RATE)
            final_pnl = sell_krw - buy_cost_krw
            current_trip_pnl += final_pnl
            hold_bars = i - buy_index
            hold_bars_list.append(hold_bars)
            cash += sell_krw
            trades.append({
                "side":           "sell",
                "signal_time":    sig_times[i],
                "execution_time": next_times[i],
                "price":          exec_price,
                "pnl":            current_trip_pnl,  # 라운드트립 합산 PnL
                "hold_bars":      hold_bars,
                "reason":         sell_reason,
            })
            asset_qty        = 0.0
            buy_price        = None
            buy_cost_krw     = 0.0
            buy_index        = None
            peak_close       = 0.0
            tp1_taken        = False
            current_trip_pnl = 0.0

        equity_curve.append(cash + asset_qty * cur_close)

    # ── 결과 지표 계산 ────────────────────────────────────────────────────
    final_close  = float(close[-1])
    final_equity = cash + asset_qty * final_close

    # partial_tp1은 round-trip 집계에서 제외 (합산 PnL은 final sell에 포함)
    sells   = [t for t in trades if t["side"] == "sell" and t.get("reason") != "partial_tp1"]
    wins    = [t for t in sells if t["pnl"] > 0]
    losses  = [t for t in sells if t["pnl"] <= 0]
    win_rate = len(wins) / len(sells) * 100.0 if sells else 0.0

    gross_win  = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = round(gross_win / gross_loss, 4) if gross_loss > 0 else 0.0

    max_drawdown = calculate_max_drawdown(equity_curve) * 100.0

    buy_hold_return = (
        (context["last_close"] / context["first_open"]) * (1.0 - FEE_RATE) ** 2 - 1.0
    ) * 100.0

    # Sharpe (연환산, 5분봉 기준)
    eq_arr = np.array(equity_curve, dtype=float)
    bar_rets = np.diff(eq_arr) / np.where(eq_arr[:-1] != 0, eq_arr[:-1], 1.0)
    bars_per_year = 365 * 24 * 12
    sharpe = 0.0
    if len(bar_rets) > 1:
        std_br = bar_rets.std()
        if std_br > 0:
            sharpe = (bar_rets.mean() / std_br) * math.sqrt(bars_per_year)

    # CAGR
    dt_kst = context["datetime_kst"]
    days_spanned = max((dt_kst.iloc[-1] - dt_kst.iloc[0]).days, 1)
    cagr = ((final_equity / INITIAL_CASH) ** (365.0 / days_spanned) - 1.0) * 100.0

    # 익스포져 (보유 캔들 비율)
    active_candles = n - 200
    exposure_pct = sum(hold_bars_list) / active_candles * 100.0 if active_candles > 0 else 0.0

    avg_hold    = float(np.mean(hold_bars_list)) if hold_bars_list else 0.0
    median_hold = float(np.median(hold_bars_list)) if hold_bars_list else 0.0

    # 연속 손실 최대
    max_consec_loss = consec = 0
    for t in sells:
        if t["pnl"] <= 0:
            consec += 1
            max_consec_loss = max(max_consec_loss, consec)
        else:
            consec = 0

    return {
        "params":                  asdict(params),
        "market":                  MARKET,
        "initial_cash":            INITIAL_CASH,
        "final_equity":            round(final_equity, 2),
        "return_pct":              round((final_equity / INITIAL_CASH - 1.0) * 100.0, 2),
        "buy_and_hold_return_pct": round(buy_hold_return, 2),
        "cagr_pct":                round(cagr, 2),
        "sharpe_per_bar":          round(sharpe, 4),
        "profit_factor":           profit_factor,
        "completed_round_trips":   len(sells),
        "win_rate_pct":            round(win_rate, 2),
        "wins":                    len(wins),
        "losses":                  len(losses),
        "max_drawdown_pct":        round(max_drawdown, 2),
        "exposure_pct":            round(exposure_pct, 2),
        "avg_hold_bars":           round(avg_hold, 2),
        "median_hold_bars":        round(median_hold, 2),
        "max_consecutive_losses":  max_consec_loss,
        "open_position":           asset_qty > 0,
        "trade_log_preview": [
            {
                "side":           t["side"],
                "signal_time":    t["signal_time"],
                "execution_time": t["execution_time"],
                "price":          round(t["price"], 2),
                "pnl":            round(t["pnl"], 2),
                "hold_bars":      t.get("hold_bars", 0),
                "reason":         t.get("reason", ""),
            }
            for t in trades[-10:]
        ],
    }


# ---------------------------------------------------------------------------
# 편의 함수
# ---------------------------------------------------------------------------

def backtest(
    params: Optional[ShortTermParams] = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    prepared_df = load_prepared_market_data(market=MARKET, days=LOOKBACK_DAYS, use_cache=use_cache)
    return run_short_term_backtest(prepared_df, params or ShortTermParams())


# ---------------------------------------------------------------------------
# 직접 실행
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import time

    print("=== 5분봉 초단기 백테스트 (단일 파라미터) ===")
    t0 = time.perf_counter()

    result = backtest()

    elapsed = time.perf_counter() - t0
    print(f"실행 시간: {elapsed:.2f}s\n")

    exclude_keys = {"params", "trade_log_preview"}
    for key, value in result.items():
        if key not in exclude_keys:
            print(f"{key:35s}: {value}")

    print("\n[파라미터]")
    for k, v in result["params"].items():
        print(f"  {k:30s}: {v}")

    print("\n[최근 10건 거래]")
    for t in result["trade_log_preview"]:
        print(f"  {t}")
