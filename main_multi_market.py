"""
KRW-XRP / KRW-ETH / KRW-BTC 5분봉 변동성 돌파 통합 라이브 트레이딩

각 시장별 검증된 winner 파라미터:
  - XRP v3   : vol=1.5  atr=1.5  vbase=10  lb=10  hold=60  tp=2.0  sl=1.5  tp1=1.2/0.7  kr_night/weekday
  - ETH F1   : vol=2.5  atr=1.0  vbase=10  lb=10  hold=60  tp=2.0  sl=1.5  tp1=off      kr_night/all
  - BTC W1   : vol=2.5  atr=2.0  vbase=10  lb=10  hold=48  tp=3.0  sl=2.0  tp1=off      kr_day/all

상태 파일:
  data_cache/xrp_night_state.json (기존 main_xrp_night.py와 호환)
  data_cache/eth_state.json
  data_cache/btc_state.json
  data_cache/top_gainer_state.json
  data_cache/top_gainer_2_state.json

종료 시 텔레그램 알림 + 시간별 통합 자산 현황 알림.
"""

import sys
import os
import math
import time
import json
import logging
import logging.config
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from apscheduler.schedulers.background import BackgroundScheduler

# ── sys.path 설정 ──────────────────────────────────────────────────────────
_dir = os.path.dirname(os.path.abspath(__file__))
for _sub in ("account", "upbit_data", "trading", "utils"):
    sys.path.append(os.path.join(_dir, _sub))

from account.my_account import get_my_exchange_account
from upbit_data.candle import get_min_candle_data
from trading.trade import buy_market, sell_market
from utils.telegram_utils import send_telegram

# ── 시장 설정 ──────────────────────────────────────────────────────────────

STATE_DIR = Path(_dir) / "data_cache"


@dataclass(frozen=True)
class MarketConfig:
    market: str           # "KRW-XRP"
    asset: str            # "XRP"
    label: str            # 화면 표시용
    state_file: Path
    # 시그널 (volatility_breakout)
    vol_mult: float
    atr_mult: float
    atr_window: int
    vol_baseline: int
    lb: int               # breakout_lookback
    # 청산
    take_profit: float
    stop_loss: float
    max_hold_bars: int
    tp1_pct: float        # 0이면 비활성
    tp1_ratio: float
    # 진입 시간 필터
    session: str          # all | kr_day | kr_night | us_open
    weekday: str          # all | weekday | weekend


XRP_CFG = MarketConfig(
    market="KRW-XRP", asset="XRP", label="XRP v3",
    state_file=STATE_DIR / "xrp_night_state.json",
    vol_mult=1.5, atr_mult=1.5, atr_window=14, vol_baseline=10, lb=10,
    take_profit=2.0, stop_loss=1.5, max_hold_bars=60,
    tp1_pct=1.2, tp1_ratio=0.7,
    session="kr_night", weekday="weekday",
)

ETH_CFG = MarketConfig(
    market="KRW-ETH", asset="ETH", label="ETH F1",
    state_file=STATE_DIR / "eth_state.json",
    vol_mult=2.5, atr_mult=1.0, atr_window=14, vol_baseline=10, lb=10,
    take_profit=2.0, stop_loss=1.5, max_hold_bars=60,
    tp1_pct=0.0, tp1_ratio=0.5,
    session="kr_night", weekday="all",
)

BTC_CFG = MarketConfig(
    market="KRW-BTC", asset="BTC", label="BTC W1",
    state_file=STATE_DIR / "btc_state.json",
    vol_mult=2.5, atr_mult=2.0, atr_window=14, vol_baseline=10, lb=10,
    take_profit=3.0, stop_loss=2.0, max_hold_bars=48,
    tp1_pct=0.0, tp1_ratio=0.5,
    session="kr_day", weekday="all",
)

MARKETS: list[MarketConfig] = [XRP_CFG, ETH_CFG, BTC_CFG]
FIXED_MARKET_CODES = {cfg.market for cfg in MARKETS}
MIN_ORDER_KRW = 5_000
UPBIT_TICKER_ALL_URL = "https://api.upbit.com/v1/ticker/all"
UPBIT_CANDLE_5M_URL = "https://api.upbit.com/v1/candles/minutes/5"
TOP_GAINER_STATE_FILE = STATE_DIR / "top_gainer_state.json"
TOP_GAINER_2_STATE_FILE = STATE_DIR / "top_gainer_2_state.json"
TOP_GAINER_STATE_FILES = {
    1: TOP_GAINER_STATE_FILE,
    2: TOP_GAINER_2_STATE_FILE,
}
TOP_GAINER_LABEL = "TOP_GAINER"
TOP_GAINER_SLOT_COUNT = 2
TOP_GAINER_MIN_CANDLES = 200
TOP_GAINER_CANDIDATE_CHECK_LIMIT = 20
TOP_GAINER_CFG_TEMPLATE = {
    "vol_mult": 2.5,
    "atr_mult": 1.0,
    "atr_window": 14,
    "vol_baseline": 10,
    "lb": 10,
    "take_profit": 2.0,
    "stop_loss": 1.5,
    "max_hold_bars": 60,
    "tp1_pct": 0.0,
    "tp1_ratio": 0.5,
    "session": "all",
    "weekday": "all",
}

_last_status_hour: int = -1
_last_dynamic_selection_hour: int = -1
_dynamic_selected_markets: dict[int, str] = {}
_dynamic_top_gainer_meta: dict[int, dict] = {}

# 1시간 동안 시장별 판단 결과 누적 (시간별 알림 발송 시 비움)
_hourly_events: dict[str, list[dict]] = {cfg.market: [] for cfg in MARKETS}


def _record_event(market: str, kind: str, time_str: str, **fields) -> None:
    """시간 활동 요약용 이벤트 기록."""
    _hourly_events.setdefault(market, []).append({
        "time": time_str, "kind": kind, **fields,
    })


def _clear_hourly_events(configs: Optional[list[MarketConfig]] = None) -> None:
    """시간별 알림 발송 후 다음 1시간 집계를 위해 판단 기록을 초기화한다."""
    for cfg in (configs or MARKETS):
        _hourly_events[cfg.market] = []

# ── 로그 설정 ──────────────────────────────────────────────────────────────
_log_dir = os.path.join(_dir, "logs")
if not os.path.exists(_log_dir):
    print(f"로그 폴더({_log_dir})가 없습니다. 생성 후 실행해주세요.")
    sys.exit(1)

logging.config.fileConfig("logging.conf")
logger = logging.getLogger(__name__)


# ── 상태 파일 ──────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {"buy_price": 0.0, "tp1_taken": False, "buy_time": None}


def load_state(cfg: MarketConfig) -> dict:
    if cfg.state_file.exists():
        try:
            return json.loads(cfg.state_file.read_text())
        except Exception:
            pass
    return _default_state()


def save_state(cfg: MarketConfig, state: dict) -> None:
    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def clear_state(cfg: MarketConfig) -> None:
    save_state(cfg, _default_state())


# ── 동적 TOP 상승률 슬롯 ──────────────────────────────────────────────────

def _market_to_asset(market: str) -> str:
    return market.split("-", 1)[1] if "-" in market else market


def _is_dynamic_cfg(cfg: MarketConfig) -> bool:
    return cfg.state_file in TOP_GAINER_STATE_FILES.values()


def _dynamic_slot(cfg: MarketConfig) -> Optional[int]:
    for slot, state_file in TOP_GAINER_STATE_FILES.items():
        if cfg.state_file == state_file:
            return slot
    return None


def _build_top_gainer_cfg(market: str, signed_change_rate: Optional[float] = None,
                          slot: int = 1) -> MarketConfig:
    """선정된 TOP 상승률 종목을 기존 전략 함수에 태우기 위한 런타임 설정."""
    label = f"TOP{slot}"
    if signed_change_rate is not None:
        label = f"TOP{slot} {signed_change_rate * 100:+.2f}%"
    return MarketConfig(
        market=market,
        asset=_market_to_asset(market),
        label=label,
        state_file=TOP_GAINER_STATE_FILES[slot],
        **TOP_GAINER_CFG_TEMPLATE,
    )


def _has_min_5m_candles(market: str, min_count: int = TOP_GAINER_MIN_CANDLES,
                        timeout_s: float = 5.0) -> bool:
    """동적 슬롯 진입 전 신규/초단기 상장 종목을 걸러낸다."""
    try:
        res = requests.get(
            UPBIT_CANDLE_5M_URL,
            params={"market": market, "count": min_count},
            headers={"Accept": "application/json"},
            timeout=timeout_s,
        )
        res.raise_for_status()
        rows = res.json()
        return isinstance(rows, list) and len(rows) >= min_count
    except Exception as e:
        logger.warning(f"[{TOP_GAINER_LABEL}] {market} 캔들 이력 확인 실패: {e}")
        return False


def get_top_krw_gainers(count: int = TOP_GAINER_SLOT_COUNT,
                        timeout_s: float = 5.0,
                        excluded_markets: Optional[set[str]] = None,
                        min_candles: int = TOP_GAINER_MIN_CANDLES) -> list[dict]:
    """
    KRW 마켓 전체에서 전일대비 등락률(signed_change_rate)이 가장 높은 종목을 반환한다.

    업비트 ticker/all 응답에는 유의종목/거래정지/가격 누락 종목이 섞일 수 있으므로
    실제 자동매매 후보로 쓰기 전에 방어적으로 제외한다. 동적 슬롯 운용 시에는
    같은 계좌 포지션을 중복 관리하지 않도록 고정 3개 시장도 제외한다. 또한
    신규 상장 직후처럼 5분봉 이력이 부족한 종목은 전략 계산이 불안정하므로 제외한다.
    """
    res = requests.get(
        UPBIT_TICKER_ALL_URL,
        params={"quote_currencies": "KRW"},
        headers={"Accept": "application/json"},
        timeout=timeout_s,
    )
    res.raise_for_status()
    rows = res.json()
    if not isinstance(rows, list):
        raise ValueError("ticker/all 응답 형식이 list가 아닙니다.")

    excluded = excluded_markets or set()
    candidates: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        market = str(row.get("market", ""))
        signed_change_rate = row.get("signed_change_rate")
        trade_price = row.get("trade_price")
        if not market.startswith("KRW-"):
            continue
        if market in excluded:
            continue
        if row.get("market_warning", "NONE") != "NONE":
            continue
        if bool(row.get("is_trading_suspended", False)):
            continue
        if signed_change_rate is None or trade_price is None:
            continue
        try:
            rate = float(signed_change_rate)
            price = float(trade_price)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        candidates.append({"market": market, "signed_change_rate": rate, "trade_price": price})

    if not candidates:
        raise ValueError("선정 가능한 KRW 상승률 후보가 없습니다.")

    selected: list[dict] = []
    selected_markets: set[str] = set()
    candidates.sort(key=lambda item: item["signed_change_rate"], reverse=True)
    for item in candidates[:TOP_GAINER_CANDIDATE_CHECK_LIMIT]:
        # 상승률 상위권 신규 상장 코인은 캔들 수가 부족해 전략 계산이 불가능할 수 있다.
        if item["market"] in selected_markets:
            continue
        if _has_min_5m_candles(item["market"], min_count=min_candles, timeout_s=timeout_s):
            selected.append(item)
            selected_markets.add(item["market"])
            if len(selected) >= count:
                return selected
            continue
        logger.info(
            f"[{TOP_GAINER_LABEL}] {item['market']} 제외: "
            f"5분봉 {min_candles}개 미만"
        )

    if selected:
        return selected
    raise ValueError(f"상승률 상위 {TOP_GAINER_CANDIDATE_CHECK_LIMIT}개 중 5분봉 {min_candles}개 이상 후보가 없습니다.")


def get_top_krw_gainer(timeout_s: float = 5.0,
                       excluded_markets: Optional[set[str]] = None,
                       min_candles: int = TOP_GAINER_MIN_CANDLES) -> dict:
    """호환용 단일 TOP 상승률 종목 조회."""
    return get_top_krw_gainers(
        count=1,
        timeout_s=timeout_s,
        excluded_markets=excluded_markets,
        min_candles=min_candles,
    )[0]


def _dynamic_reason_line(cfg: MarketConfig) -> str:
    if not _is_dynamic_cfg(cfg):
        return ""
    slot = _dynamic_slot(cfg) or 1
    rate = _dynamic_top_gainer_meta.get(slot, {}).get("signed_change_rate")
    if rate is None:
        return f"선정사유: KRW 마켓 전일대비 상승률 TOP{slot} (고정 3개 시장 제외)"
    return f"선정사유: KRW 마켓 전일대비 상승률 TOP{slot} ({rate * 100:+.2f}%, 고정 3개 시장 제외)"


def _select_dynamic_markets(now: datetime, account: dict) -> list[MarketConfig]:
    """1시간마다 TOP 상승률 2개 종목을 갱신하되, 보유 중인 슬롯은 기존 포지션을 유지한다."""
    global _last_dynamic_selection_hour, _dynamic_selected_markets, _dynamic_top_gainer_meta

    active: list[MarketConfig] = []
    occupied_markets = set(FIXED_MARKET_CODES)
    refresh_slots: list[int] = []

    for slot in range(1, TOP_GAINER_SLOT_COUNT + 1):
        state = load_state(_build_top_gainer_cfg(f"KRW-TOP{slot}", slot=slot))
        state_market = state.get("market")
        if state.get("buy_price", 0) > 0 and state_market:
            held_cfg = _build_top_gainer_cfg(
                str(state_market),
                state.get("signed_change_rate"),
                slot=slot,
            )
            if has_position(account, held_cfg):
                _dynamic_selected_markets[slot] = held_cfg.market
                _dynamic_top_gainer_meta[slot] = {
                    "market": held_cfg.market,
                    "signed_change_rate": state.get("signed_change_rate"),
                    "trade_price": state.get("selected_trade_price"),
                    "selected_at": state.get("selected_at"),
                    "status": "holding",
                }
                active.append(held_cfg)
                occupied_markets.add(held_cfg.market)
                continue
        refresh_slots.append(slot)

    if not refresh_slots:
        return active

    if now.hour == _last_dynamic_selection_hour:
        for slot in refresh_slots:
            market = _dynamic_selected_markets.get(slot)
            if market and market not in occupied_markets:
                meta = _dynamic_top_gainer_meta.get(slot, {})
                active.append(_build_top_gainer_cfg(
                    market,
                    meta.get("signed_change_rate"),
                    slot=slot,
                ))
                occupied_markets.add(market)
        return active

    try:
        tops = get_top_krw_gainers(
            count=len(refresh_slots),
            excluded_markets=occupied_markets,
        )
        _last_dynamic_selection_hour = now.hour
        for slot, top in zip(refresh_slots, tops):
            _dynamic_selected_markets[slot] = top["market"]
            _dynamic_top_gainer_meta[slot] = {
                **top,
                "selected_at": now.isoformat(),
                "status": "selected",
            }
            occupied_markets.add(top["market"])
            logger.info(
                f"[{TOP_GAINER_LABEL}{slot}] selected {top['market']} "
                f"rate={top['signed_change_rate'] * 100:+.2f}% price={top['trade_price']:.8f}"
            )
            active.append(_build_top_gainer_cfg(top["market"], top["signed_change_rate"], slot=slot))
        return active
    except Exception as e:
        logger.warning(f"[{TOP_GAINER_LABEL}] 상승률 TOP2 선정 실패: {e}")
        _last_dynamic_selection_hour = now.hour
        for slot in refresh_slots:
            market = _dynamic_selected_markets.get(slot)
            if market and market not in occupied_markets:
                meta = _dynamic_top_gainer_meta.setdefault(slot, {})
                meta["status"] = "fallback"
                meta["last_error"] = str(e)
                active.append(_build_top_gainer_cfg(
                    market,
                    meta.get("signed_change_rate"),
                    slot=slot,
                ))
                occupied_markets.add(market)
            else:
                _dynamic_top_gainer_meta[slot] = {"status": "failed", "last_error": str(e)}
        return active


# ── 인디케이터 ─────────────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame, cfg: MarketConfig) -> dict:
    """1000봉 DataFrame(오름차순)에서 마지막 완성 캔들(iloc[-2]) 기준 시그널."""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / cfg.atr_window, adjust=False, min_periods=cfg.atr_window).mean()

    vol_ma = volume.rolling(cfg.vol_baseline, min_periods=1).mean()
    vol_ratio = (volume / vol_ma).replace([float("inf"), float("-inf")], 0).fillna(0)

    rolling_high_prev = high.shift(1).rolling(cfg.lb, min_periods=1).max()
    breakout_threshold = rolling_high_prev + atr * cfg.atr_mult

    i = -2
    return {
        "close": float(close.iloc[i]),
        "vol_ratio": float(vol_ratio.iloc[i]),
        "bt": float(breakout_threshold.iloc[i]),
        "atr": float(atr.iloc[i]),
    }


# ── 계좌 ───────────────────────────────────────────────────────────────────

def get_account_info() -> dict:
    """KRW + 모든 보유 코인 잔고를 dict로 반환."""
    acct = get_my_exchange_account()
    info: dict = {"krw_balance": 0.0}
    for _, row in acct.iterrows():
        cur = row["currency"]
        if cur == "KRW":
            info["krw_balance"] = float(row["balance"])
        else:
            info[cur] = {
                "balance": str(row["balance"]),
                "avg_buy_price": float(row["avg_buy_price"]),
            }
    info["krw_available"] = math.floor(info["krw_balance"] * 0.999)
    return info


def has_position(account: dict, cfg: MarketConfig, price: Optional[float] = None) -> bool:
    """최소 매도 가능 금액 미만의 소량 잔고는 포지션으로 보지 않는다."""
    if cfg.asset not in account:
        return False

    balance = float(account[cfg.asset]["balance"])
    if balance <= 0:
        return False

    ref_price = price if price is not None else float(account[cfg.asset]["avg_buy_price"])
    return balance * ref_price >= MIN_ORDER_KRW


def _position_value_krw(account: dict, cfg: MarketConfig, price: float) -> float:
    if cfg.asset not in account:
        return 0.0
    return float(account[cfg.asset]["balance"]) * price


# ── 필터 / 유틸 ────────────────────────────────────────────────────────────

def passes_filter(now: datetime, cfg: MarketConfig) -> bool:
    if cfg.weekday == "weekday" and now.weekday() >= 5:
        return False
    if cfg.weekday == "weekend" and now.weekday() < 5:
        return False
    h = now.hour
    if cfg.session == "all":
        return True
    if cfg.session == "kr_night":
        return 0 <= h < 9
    if cfg.session == "kr_day":
        return 9 <= h < 18
    if cfg.session == "us_open":
        return h >= 22 or h < 2
    return False


def hold_bars_elapsed(buy_time_iso: Optional[str], now: datetime) -> int:
    if not buy_time_iso:
        return 0
    try:
        buy_dt = datetime.fromisoformat(buy_time_iso)
    except Exception:
        return 0
    return int((now - buy_dt).total_seconds() // 300)


def truncate_qty(qty: float, decimals: int = 8) -> str:
    factor = 10 ** decimals
    return str(math.floor(qty * factor) / factor)


def _check_order(res: pd.DataFrame) -> tuple[bool, str]:
    """(성공여부, 실패사유) 반환."""
    try:
        if "uuid" in res.columns and pd.notnull(res["uuid"].iloc[0]):
            return True, ""
        if "error" in res.columns:
            err = res["error"].iloc[0]
            if isinstance(err, dict):
                return False, err.get("message", str(err))
            return False, str(err)
        return False, "uuid 미반환"
    except Exception as e:
        return False, str(e)


# ── 텔레그램 메시지 helpers ────────────────────────────────────────────────

def _ts_short(now: datetime) -> str:
    return now.strftime("%H:%M:%S KST")


def _ts_full(now: datetime) -> str:
    return now.strftime("%Y-%m-%d %H:%M:%S KST")


def _fmt_price(p: float) -> str:
    """1000원 이상은 정수, 미만은 소수 둘째자리."""
    if p >= 1000:
        return f"{p:,.0f}원"
    return f"{p:,.2f}원"


def _fmt_pnl(pnl_pct: float, pnl_krw: float) -> str:
    return f"{pnl_pct:+.2f}% ({pnl_krw:+,.0f}원)"


def _fmt_hold(bars: int) -> str:
    h, m = bars * 5 // 60, bars * 5 % 60
    if h > 0 and m > 0:
        return f"{h}시간 {m}분 ({bars}봉)"
    if h > 0:
        return f"{h}시간 ({bars}봉)"
    return f"{m}분 ({bars}봉)"


def _fmt_session(cfg: MarketConfig) -> str:
    sess = {
        "all": "24/7 (전체)",
        "kr_night": "00-09시 KST",
        "kr_day": "09-18시 KST",
        "us_open": "22-02시 KST",
    }.get(cfg.session, cfg.session)
    week = {"all": "전체", "weekday": "평일", "weekend": "주말"}.get(cfg.weekday, cfg.weekday)
    return f"{sess} / {week}"


# ── 텔레그램 메시지: 매매 알림 ─────────────────────────────────────────────

def _msg_buy(cfg: MarketConfig, now: datetime, buy_price: float,
             krw_used: int, qty: float, krw_remaining: float) -> str:
    lines = [
        f"🟢 매수 체결 — {cfg.market} ({cfg.label})",
        f"시각: {_ts_short(now)}",
        f"매수가: {_fmt_price(buy_price)}",
        f"수량: {qty:.4f} {cfg.asset}",
        f"투자금: {krw_used:,}원 (현재 가용 KRW 한도)",
        f"잔여 KRW: {krw_remaining:,.0f}원",
    ]
    reason = _dynamic_reason_line(cfg)
    if reason:
        lines.append(reason)
    return "\n".join(lines)


def _msg_sell(cfg: MarketConfig, now: datetime, reason_label: str, icon: str,
              sell_price: float, buy_price: float, hold_bars: int,
              pnl_pct: float, pnl_krw: float, extra: str = "") -> str:
    lines = [
        f"{icon} {reason_label} — {cfg.market} ({cfg.label})",
        f"시각: {_ts_short(now)}",
        f"매도가: {_fmt_price(sell_price)}  매수가: {_fmt_price(buy_price)}",
        f"보유: {_fmt_hold(hold_bars)}",
        f"PnL: {_fmt_pnl(pnl_pct, pnl_krw)}",
    ]
    if extra:
        lines.append(extra)
    reason = _dynamic_reason_line(cfg)
    if reason:
        lines.append(reason)
    return "\n".join(lines)


def _msg_fail(cfg: MarketConfig, now: datetime, action: str, reason: str) -> str:
    return "\n".join([
        f"⚠️ {action} 실패 — {cfg.market} ({cfg.label})",
        f"시각: {_ts_short(now)}",
        f"사유: {reason}",
        "(수동 확인 필요)",
    ])


def _msg_error(cfg: MarketConfig, now: datetime, e: Exception) -> str:
    return "\n".join([
        f"🚨 오류 — {cfg.market} ({cfg.label})",
        f"시각: {_ts_short(now)}",
        f"{type(e).__name__}: {e}",
    ])


# ── 텔레그램 메시지: 시간별 통합 현황 ──────────────────────────────────────

def _market_status_block(cfg: MarketConfig, now: datetime, account: dict,
                          state: dict, cur_price: float, indic: dict) -> str:
    lines = [f"━ {cfg.market} ({cfg.label}) ━"]
    if _is_dynamic_cfg(cfg):
        slot = _dynamic_slot(cfg) or 1
        meta = _dynamic_top_gainer_meta.get(slot, {})
        selected_at = meta.get("selected_at")
        rate = meta.get("signed_change_rate")
        selected_text = "선정시각: -"
        if selected_at:
            try:
                selected_text = f"선정시각: {_ts_short(datetime.fromisoformat(selected_at))}"
            except Exception:
                selected_text = f"선정시각: {selected_at}"
        if rate is not None:
            lines.append(f"동적슬롯 TOP{slot}: 전일대비 {rate * 100:+.2f}% (고정 제외) / {selected_text}")
        else:
            lines.append(f"동적슬롯 TOP{slot}: 전일대비 상승률 상위권(고정 3개 시장 제외) / {selected_text}")
        if meta.get("status") == "fallback":
            lines.append(f"선정상태: API 실패로 직전 선정 유지 ({meta.get('last_error', '')[:40]})")
    if has_position(account, cfg, cur_price):
        bal = float(account[cfg.asset]["balance"])
        avg = account[cfg.asset]["avg_buy_price"]
        buy_price = state.get("buy_price") or avg
        tp1_taken = bool(state.get("tp1_taken", False))
        hold_bars = hold_bars_elapsed(state.get("buy_time"), now)
        pnl_pct = (cur_price / buy_price - 1) * 100 if buy_price > 0 else 0.0
        pnl_krw = (cur_price - buy_price) * bal if buy_price > 0 else 0.0
        sl_p = buy_price * (1.0 - cfg.stop_loss / 100.0)
        tp_p = buy_price * (1.0 + cfg.take_profit / 100.0)
        lines.append(f"🔵 LONG  P&L: {_fmt_pnl(pnl_pct, pnl_krw)}")
        lines.append(f"가격: {_fmt_price(cur_price)}  매수: {_fmt_price(buy_price)}")
        lines.append(f"보유: {_fmt_hold(hold_bars)} / 한도 {cfg.max_hold_bars}봉")
        lines.append(f"SL: {_fmt_price(sl_p)}  TP: {_fmt_price(tp_p)}")
        if cfg.tp1_pct > 0:
            tp1_p = buy_price * (1.0 + cfg.tp1_pct / 100.0)
            tp1_status = "✓ 완료" if tp1_taken else "대기"
            lines.append(f"TP1: {_fmt_price(tp1_p)}  ({tp1_status})")
        lines.append(f"수량: {bal:.4f} {cfg.asset}  평가: {bal*cur_price:,.0f}원")
    else:
        in_session = passes_filter(now, cfg)
        ses_label = "✅ 진입가능" if in_session else f"⏸ 세션외 ({_fmt_session(cfg)})"
        lines.append(f"⚪ NONE  {ses_label}")
        lines.append(f"가격: {_fmt_price(cur_price)}")
        if in_session:
            gap_pct = (indic["bt"] / cur_price - 1) * 100 if cur_price > 0 else 0.0
            price_ok = "🟢" if cur_price > indic["bt"] else "🔴"
            vol_ok = "🟢" if indic["vol_ratio"] >= cfg.vol_mult else "🔴"
            lines.append(f"매수한도: 현재 가용 KRW {account['krw_available']:,.0f}원")
            lines.append(f"돌파임계: {_fmt_price(indic['bt'])} ({gap_pct:+.2f}%)")
            lines.append(f"시그널: {price_ok}가격  {vol_ok}거래량 "
                         f"({indic['vol_ratio']:.2f}x / {cfg.vol_mult}x)")
    return "\n".join(lines)


def _hourly_activity_block(cfg: MarketConfig) -> str:
    """1시간 동안 시장의 판단/거래 결과 요약."""
    events = _hourly_events.get(cfg.market, [])
    if not events:
        return f"━ {cfg.market} ━\n투자 판단 기록 없음"

    total = len(events)
    in_session = sum(1 for e in events if e["kind"] != "OUT_OF_SESSION")
    hold_count = sum(1 for e in events if e["kind"] == "HOLD")
    no_sig_count = sum(1 for e in events if e["kind"] == "NO_SIGNAL")
    oos_count = sum(1 for e in events if e["kind"] == "OUT_OF_SESSION")

    sell_kinds = {"SELL_SL", "SELL_TP", "SELL_TP1", "SELL_TIME", "SELL_FAIL"}
    trade_kinds = sell_kinds | {"BUY", "BUY_FAIL", "BUY_BLOCKED"}
    trades = [e for e in events if e["kind"] in trade_kinds]

    lines = [f"━ {cfg.market} ━ 판단 {total}회 (세션내 {in_session}회)"]
    lines.append(
        f"요약: 거래/시도 {len(trades)}회, 보유유지 {hold_count}회, "
        f"무신호 {no_sig_count}회, 세션외 {oos_count}회"
    )

    if trades:
        # 거래 이벤트가 많아져도 텔레그램 메시지가 과도하게 길어지지 않도록 최근 항목만 상세 표시한다.
        recent_trades = trades[-8:]
        if len(trades) > len(recent_trades):
            lines.append(f"최근 거래/시도 {len(recent_trades)}건만 표시")
        for t in recent_trades:
            tm = t["time"]
            kind = t["kind"]
            if kind == "BUY":
                lines.append(f"🟢 {tm} 매수 @ {_fmt_price(t['price'])}")
            elif kind == "BUY_FAIL":
                lines.append(f"⚠️ {tm} 매수실패 ({t.get('detail','')[:40]})")
            elif kind == "BUY_BLOCKED":
                lines.append(f"⚠️ {tm} 매수보류 (KRW 부족)")
            elif kind == "SELL_SL":
                lines.append(f"❌ {tm} 손절 @ {_fmt_price(t['price'])} "
                             f"({_fmt_pnl(t['pnl_pct'], t['pnl_krw'])})")
            elif kind == "SELL_TP":
                lines.append(f"✅ {tm} 익절 @ {_fmt_price(t['price'])} "
                             f"({_fmt_pnl(t['pnl_pct'], t['pnl_krw'])})")
            elif kind == "SELL_TP1":
                lines.append(f"🎯 {tm} TP1 @ {_fmt_price(t['price'])} "
                             f"({_fmt_pnl(t['pnl_pct'], t['pnl_krw'])})")
            elif kind == "SELL_TIME":
                lines.append(f"⏰ {tm} 시간청산 @ {_fmt_price(t['price'])} "
                             f"({_fmt_pnl(t['pnl_pct'], t['pnl_krw'])})")
            elif kind == "SELL_FAIL":
                lines.append(f"⚠️ {tm} 매도실패 ({t.get('detail','')[:40]})")
    else:
        if hold_count == total:
            last = events[-1]
            lines.append(f"보유 유지 {hold_count}회  현재 PnL: {last.get('pnl_pct', 0):+.2f}%")
        elif oos_count == total:
            lines.append("세션 외 — 거래 없음")
        elif no_sig_count > 0:
            # 가장 가까웠던 진입 근접도
            no_sigs = [e for e in events if e["kind"] == "NO_SIGNAL"]
            best = min(no_sigs, key=lambda e: abs(e.get("bt_gap_pct", 999)))
            best_vr = max((e.get("vol_ratio", 0) for e in no_sigs), default=0)
            lines.append(f"진입신호 0회 / 무포지션 대기 {no_sig_count}회")
            lines.append(f"가장 근접: 돌파임계 대비 {best.get('bt_gap_pct', 0):+.2f}%, "
                         f"최고 거래량비 {best_vr:.2f}x (기준 {cfg.vol_mult}x)")
        else:
            mix = []
            if hold_count: mix.append(f"보유 {hold_count}")
            if no_sig_count: mix.append(f"무신호 {no_sig_count}")
            if oos_count: mix.append(f"세션외 {oos_count}")
            lines.append(" / ".join(mix) if mix else "활동 없음")

    return "\n".join(lines)


def _combined_status_msg(now: datetime, snapshots: dict, krw_balance: float,
                         configs: Optional[list[MarketConfig]] = None) -> str:
    lines = [f"📊 자산현황 — {_ts_full(now)}", ""]
    asset_value_total = 0.0
    active_configs = configs or MARKETS
    active_dynamic_slots = {
        slot for cfg in active_configs
        if (slot := _dynamic_slot(cfg)) is not None
    }
    for cfg in active_configs:
        snap = snapshots.get(cfg.market)
        if snap is None:
            lines.append(f"━ {cfg.market} ({cfg.label}) ━")
            lines.append("(데이터 미수집)")
            lines.append("")
            continue
        account, state, cur_price, indic = snap
        lines.append(_market_status_block(cfg, now, account, state, cur_price, indic))
        lines.append("")
        if has_position(account, cfg, cur_price):
            asset_value_total += float(account[cfg.asset]["balance"]) * cur_price
    failed = [
        (slot, meta) for slot, meta in _dynamic_top_gainer_meta.items()
        if meta.get("status") == "failed" and slot not in active_dynamic_slots
    ]
    if failed:
        lines.append("━ 동적 슬롯 (TOP_GAINER) ━")
        for slot, meta in failed:
            lines.append(f"TOP{slot} 선정 실패: {meta.get('last_error', '')[:80]}")
        lines.append("")

    # 자산 합계
    lines.append("━━━ 자산 합계 ━━━")
    lines.append(f"💰 KRW: {krw_balance:,.0f}원")
    lines.append(f"💼 코인평가: {asset_value_total:,.0f}원")
    lines.append(f"📊 총평가: {asset_value_total + krw_balance:,.0f}원")
    lines.append("")

    # 1시간 투자 판단 요약
    lines.append("━━━ 지난 1시간 투자 판단 요약 ━━━")
    for cfg in active_configs:
        lines.append(_hourly_activity_block(cfg))
    return "\n".join(lines)


# ── 텔레그램 메시지: 시작 / 종료 ───────────────────────────────────────────

def _startup_msg(now: datetime) -> str:
    parts = [f"🤖 Multi-Market 봇 시작", f"시작: {_ts_full(now)}", ""]
    for cfg in MARKETS:
        tp1_str = (f"  TP1: {cfg.tp1_pct}% × {cfg.tp1_ratio*100:.0f}% 부분익절"
                   if cfg.tp1_pct > 0 else "  TP1: 비활성")
        parts.extend([
            f"━ {cfg.market} ({cfg.label}) ━",
            f"  진입시간: {_fmt_session(cfg)}",
            f"  시그널: vol×{cfg.vol_mult}, atr×{cfg.atr_mult}, lb={cfg.lb}, vbase={cfg.vol_baseline}",
            f"  청산: TP={cfg.take_profit}%, SL={cfg.stop_loss}%, 보유한도={cfg.max_hold_bars}봉 ({cfg.max_hold_bars*5/60:.0f}h)",
            tp1_str,
            "  매수한도: 현재 가용 KRW 잔고 전체",
            "",
        ])
    parts.append("총 매수한도: 고정 한도 없음 (신호 발생 시점의 현재 가용 KRW)")
    parts.extend([
        "",
        "━ 동적 슬롯 (TOP_GAINER ×2) ━",
        "  대상: KRW 마켓 전일대비 상승률 TOP 2 (고정 3개 시장 제외)",
        "  갱신: 1시간마다 재선정",
        "  보유정책: 슬롯별 보유 중 강제 교체 없음 (TP/SL/시간청산까지 유지)",
        "  전략: 거래기회 확대형 5분봉 변동성 돌파 (vol×2.5, atr×1.0, TP=2.0%, SL=1.5%)",
    ])
    return "\n".join(parts)


def _shutdown_msg(now: datetime) -> str:
    return f"🛑 Multi-Market 봇 종료\n종료: {_ts_full(now)}"


# ── 시장별 트레이딩 ────────────────────────────────────────────────────────

def _fetch_candles_with_retry(market: str, attempts: int = 3, sleep_s: float = 1.0) -> pd.DataFrame:
    """get_min_candle_data를 재시도. 멀티 마켓 호출 시 rate limit 안정성 위함."""
    last_err: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return get_min_candle_data(market, 5)
        except Exception as e:
            last_err = e
            logger.warning(f"[{market}] candle fetch 실패 ({attempt+1}/{attempts}): {e}")
            if attempt < attempts - 1:
                time.sleep(sleep_s * (attempt + 1))   # 1s, 2s, ...
    raise last_err if last_err else RuntimeError("candle fetch unknown error")


def trade_one_market(cfg: MarketConfig, now: datetime, account: dict
                     ) -> tuple[dict, dict, float, dict]:
    """
    단일 시장 1회 처리. 매수/매도 발생 시 account 재조회해 반환.
    returns (account, state, cur_price, indic)
    """
    state = load_state(cfg)
    candles = _fetch_candles_with_retry(cfg.market)
    indic = compute_indicators(candles, cfg)
    cur_price = indic["close"]

    do_trade = (now.minute % 5 == 0)
    if not do_trade:
        return account, state, cur_price, indic

    time_str = now.strftime("%H:%M")

    pos = has_position(account, cfg, cur_price)
    logger.info(
        f"[{cfg.market}] pos={'LONG' if pos else 'NONE'}  close={cur_price:.5f}  "
        f"vol_ratio={indic['vol_ratio']:.2f}  bt={indic['bt']:.5f}"
    )

    # ─────────────── 포지션 있음 ───────────────
    if pos:
        bal_str = account[cfg.asset]["balance"]
        avg = account[cfg.asset]["avg_buy_price"]
        buy_price = state.get("buy_price") or avg
        tp1_taken = bool(state.get("tp1_taken", False))
        hold_bars = hold_bars_elapsed(state.get("buy_time"), now)

        qty_total = float(bal_str)
        pnl_pct = (cur_price / buy_price - 1) * 100 if buy_price > 0 else 0.0
        pnl_krw_total = (cur_price - buy_price) * qty_total if buy_price > 0 else 0.0

        # 1. 손절
        if cur_price <= buy_price * (1.0 - cfg.stop_loss / 100.0):
            logger.info(f"[{cfg.market}] [SELL] 손절  price={cur_price:.5f}  buy={buy_price:.5f}")
            res = sell_market(cfg.market, bal_str)
            ok, reason = _check_order(res)
            if ok:
                send_telegram(_msg_sell(cfg, now, "손절 체결", "❌",
                                        cur_price, buy_price, hold_bars,
                                        pnl_pct, pnl_krw_total))
                _record_event(cfg.market, "SELL_SL", time_str,
                              price=cur_price, pnl_pct=pnl_pct, pnl_krw=pnl_krw_total)
                clear_state(cfg)
                account = get_account_info()
            else:
                logger.error(f"[{cfg.market}] 손절 실패: {reason}")
                send_telegram(_msg_fail(cfg, now, "손절", reason))
                _record_event(cfg.market, "SELL_FAIL", time_str, detail=reason)
            return account, load_state(cfg), cur_price, indic

        # 2. 전량 익절
        if cur_price >= buy_price * (1.0 + cfg.take_profit / 100.0):
            logger.info(f"[{cfg.market}] [SELL] 전량익절  price={cur_price:.5f}  buy={buy_price:.5f}")
            res = sell_market(cfg.market, bal_str)
            ok, reason = _check_order(res)
            if ok:
                send_telegram(_msg_sell(cfg, now, "익절 체결 (전량)", "✅",
                                        cur_price, buy_price, hold_bars,
                                        pnl_pct, pnl_krw_total))
                _record_event(cfg.market, "SELL_TP", time_str,
                              price=cur_price, pnl_pct=pnl_pct, pnl_krw=pnl_krw_total)
                clear_state(cfg)
                account = get_account_info()
            else:
                logger.error(f"[{cfg.market}] 익절 실패: {reason}")
                send_telegram(_msg_fail(cfg, now, "익절", reason))
                _record_event(cfg.market, "SELL_FAIL", time_str, detail=reason)
            return account, load_state(cfg), cur_price, indic

        # 3. TP1 부분 익절 (활성 시)
        if cfg.tp1_pct > 0 and not tp1_taken \
                and cur_price >= buy_price * (1.0 + cfg.tp1_pct / 100.0):
            tp1_qty = qty_total * cfg.tp1_ratio
            tp1_est_krw = tp1_qty * cur_price
            if tp1_est_krw >= MIN_ORDER_KRW:
                sell_qty_str = truncate_qty(tp1_qty)
                logger.info(
                    f"[{cfg.market}] [SELL] TP1 {cfg.tp1_ratio*100:.0f}%  qty={sell_qty_str}"
                )
                res = sell_market(cfg.market, sell_qty_str)
                ok, reason = _check_order(res)
                if ok:
                    pnl_krw_part = (cur_price - buy_price) * tp1_qty
                    remain = qty_total - tp1_qty
                    label = f"TP1 부분익절 {cfg.tp1_ratio*100:.0f}%"
                    extra = f"잔여: {remain:.4f} {cfg.asset}"
                    send_telegram(_msg_sell(cfg, now, label, "🎯",
                                            cur_price, buy_price, hold_bars,
                                            pnl_pct, pnl_krw_part, extra))
                    _record_event(cfg.market, "SELL_TP1", time_str,
                                  price=cur_price, pnl_pct=pnl_pct, pnl_krw=pnl_krw_part)
                    state["tp1_taken"] = True
                    save_state(cfg, state)
                    account = get_account_info()
                else:
                    logger.error(f"[{cfg.market}] TP1 실패: {reason}")
                    send_telegram(_msg_fail(cfg, now, "TP1", reason))
                    _record_event(cfg.market, "SELL_FAIL", time_str, detail=reason)
            else:
                logger.warning(
                    f"[{cfg.market}] TP1 잔여 금액({tp1_est_krw:.0f}원) < 최소주문, 건너뜀"
                )
                _record_event(cfg.market, "HOLD", time_str,
                              price=cur_price, pnl_pct=pnl_pct)
            return account, load_state(cfg), cur_price, indic

        # 4. 시간 청산
        if hold_bars >= cfg.max_hold_bars:
            logger.info(f"[{cfg.market}] [SELL] 시간청산  hold_bars={hold_bars}/{cfg.max_hold_bars}")
            res = sell_market(cfg.market, bal_str)
            ok, reason = _check_order(res)
            if ok:
                send_telegram(_msg_sell(cfg, now, "시간청산", "⏰",
                                        cur_price, buy_price, hold_bars,
                                        pnl_pct, pnl_krw_total))
                _record_event(cfg.market, "SELL_TIME", time_str,
                              price=cur_price, pnl_pct=pnl_pct, pnl_krw=pnl_krw_total)
                clear_state(cfg)
                account = get_account_info()
            else:
                logger.error(f"[{cfg.market}] 시간청산 실패: {reason}")
                send_telegram(_msg_fail(cfg, now, "시간청산", reason))
                _record_event(cfg.market, "SELL_FAIL", time_str, detail=reason)
            return account, load_state(cfg), cur_price, indic

        logger.info(f"[{cfg.market}] 보유중 — 청산조건 없음")
        _record_event(cfg.market, "HOLD", time_str,
                      price=cur_price, pnl_pct=pnl_pct)
        return account, state, cur_price, indic

    # ─────────────── 포지션 없음 ───────────────
    if state.get("buy_price", 0) > 0:
        logger.info(f"[{cfg.market}] 포지션없음 + 상태파일 잔존 → 초기화")
        clear_state(cfg)
        state = _default_state()

    if not passes_filter(now, cfg):
        logger.debug(f"[{cfg.market}] 세션/요일 필터 미통과")
        _record_event(cfg.market, "OUT_OF_SESSION", time_str)
        return account, state, cur_price, indic

    entry = (cur_price > indic["bt"]) and (indic["vol_ratio"] >= cfg.vol_mult)
    logger.info(
        f"[{cfg.market}] 진입신호={entry}  "
        f"(close={cur_price:.5f} vs bt={indic['bt']:.5f}, "
        f"vol_ratio={indic['vol_ratio']:.2f} vs {cfg.vol_mult})"
    )
    if not entry:
        bt_gap_pct = (indic["bt"] / cur_price - 1) * 100 if cur_price > 0 else 0.0
        _record_event(cfg.market, "NO_SIGNAL", time_str,
                      price=cur_price, vol_ratio=indic["vol_ratio"], bt_gap_pct=bt_gap_pct)
        return account, state, cur_price, indic

    # 매수 가능 KRW 결정: 고정 시장 한도 없이 현재 가용 KRW 잔고를 사용한다.
    krw_use = account["krw_available"]
    if krw_use < MIN_ORDER_KRW:
        msg = f"투자가능금액({krw_use:,}원) < 최소주문({MIN_ORDER_KRW:,}원)"
        logger.warning(f"[{cfg.market}] {msg}")
        send_telegram(_msg_fail(cfg, now, "매수", msg))
        _record_event(cfg.market, "BUY_BLOCKED", time_str, detail=msg)
        return account, state, cur_price, indic

    res = buy_market(cfg.market, krw_use)
    ok, reason = _check_order(res)
    if ok:
        time.sleep(2)
        # 체결 후 계좌 재조회로 정확한 매수가·수량 확보
        account = get_account_info()
        if cfg.asset in account:
            actual_buy = account[cfg.asset]["avg_buy_price"]
            actual_qty = float(account[cfg.asset]["balance"])
        else:
            actual_buy = cur_price
            actual_qty = krw_use * 0.9995 / cur_price  # 추정치 (수수료 0.05%)

        new_state = {
            "buy_price": actual_buy,
            "tp1_taken": False,
            "buy_time": now.isoformat(),
        }
        if _is_dynamic_cfg(cfg):
            # 동적 슬롯은 재시작 후에도 기존 보유 종목을 계속 관리해야 하므로
            # 매수 당시 선정 정보를 상태 파일에 함께 남긴다.
            slot = _dynamic_slot(cfg) or 1
            meta = _dynamic_top_gainer_meta.get(slot, {})
            new_state.update({
                "market": cfg.market,
                "asset": cfg.asset,
                "slot": slot,
                "signed_change_rate": meta.get("signed_change_rate"),
                "selected_trade_price": meta.get("trade_price"),
                "selected_at": meta.get("selected_at"),
            })
        save_state(cfg, new_state)
        send_telegram(_msg_buy(cfg, now, actual_buy, krw_use, actual_qty,
                                account["krw_balance"]))
        _record_event(cfg.market, "BUY", time_str,
                      price=actual_buy, krw_used=krw_use)
        logger.info(f"[{cfg.market}] [BUY] {krw_use:,}원 매수 완료  avg_buy_price={actual_buy:.5f}")
    else:
        logger.error(f"[{cfg.market}] 매수 실패: {reason}")
        send_telegram(_msg_fail(cfg, now, "매수", reason))
        _record_event(cfg.market, "BUY_FAIL", time_str, detail=reason)

    return account, load_state(cfg), cur_price, indic


# ── 메인 루프 ──────────────────────────────────────────────────────────────

def auto_trading() -> None:
    try:
        now = datetime.now()
        account = get_account_info()
        snapshots: dict = {}
        active_configs = list(MARKETS)

        active_configs.extend(_select_dynamic_markets(now, account))

        for idx, cfg in enumerate(active_configs):
            if idx > 0:
                time.sleep(0.3)   # 시장 간 호출 분산 (rate limit 완화)
            try:
                account, state, cur_price, indic = trade_one_market(cfg, now, account)
                snapshots[cfg.market] = (account, state, cur_price, indic)
            except Exception as e:
                logger.error(f"[{cfg.market}] 트레이딩 오류: {e}", exc_info=True)
                send_telegram(_msg_error(cfg, now, e))

        # 매시간 통합 자산 현황
        global _last_status_hour
        if now.hour != _last_status_hour:
            try:
                msg = _combined_status_msg(now, snapshots, account["krw_balance"], active_configs)
                if send_telegram(msg):
                    _last_status_hour = now.hour
                    _clear_hourly_events(active_configs)
                else:
                    logger.warning("hourly status 텔레그램 전송 실패")
            except Exception as e:
                logger.warning(f"hourly status 실패: {e}")

    except ValueError as ve:
        logger.error(f"ValueError: {ve}")
    except Exception as e:
        logger.error(f"예상치 못한 오류: {e}", exc_info=True)


# ── 진입점 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("++++++++++ Multi-Market 5분봉 (XRP/ETH/BTC) starts. ++++++++++")
    for cfg in MARKETS:
        logger.info(
            f"  {cfg.market} ({cfg.label}): vol={cfg.vol_mult} atr={cfg.atr_mult} "
            f"vbase={cfg.vol_baseline} lb={cfg.lb}  "
            f"hold={cfg.max_hold_bars}  TP={cfg.take_profit}% SL={cfg.stop_loss}%  "
            f"tp1={cfg.tp1_pct}/{cfg.tp1_ratio}  ses={cfg.session}/{cfg.weekday}  "
            "max_buy=current_available_krw"
        )
    logger.info(
        "  TOP_GAINER x2: KRW ticker/all signed_change_rate TOP2, hourly refresh, "
        "no forced rotation while holding"
    )

    now = datetime.now()
    send_telegram(_startup_msg(now))

    scheduler = BackgroundScheduler()
    scheduler.add_job(auto_trading, "cron", second=5)
    scheduler.start()

    try:
        while True:
            time.sleep(2)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("스케줄러 종료")
        send_telegram(_shutdown_msg(datetime.now()))
