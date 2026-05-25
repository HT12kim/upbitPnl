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
from datetime import datetime, timedelta
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
from trading.trade import buy_market, sell_market
from utils.telegram_utils import send_telegram
from live_signal import LiveSignalParams, evaluate_live_signal

# ── 시장 설정 ──────────────────────────────────────────────────────────────

STATE_DIR = Path(_dir) / "data_cache"
STRATEGY_WINNERS_PATH = Path(_dir) / "strategy_winners.json"


def _load_strategy_winners() -> dict:
    """백테스트 winner 설정 파일을 읽는다. 실패 시 하드코딩 baseline으로 동작한다."""
    if not STRATEGY_WINNERS_PATH.exists():
        return {}
    try:
        payload = json.loads(STRATEGY_WINNERS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"strategy_winners.json 로드 실패, baseline 사용: {e}")
        return {}
    configs = payload.get("market_configs", {})
    return configs if isinstance(configs, dict) else {}


_STRATEGY_WINNERS = _load_strategy_winners()


def _winner_value(profile: str, key: str, default):
    cfg = _STRATEGY_WINNERS.get(profile, {})
    if not isinstance(cfg, dict):
        return default
    return cfg.get(key, default)


def _winner_nested_value(profile: str, section: str, key: str, default):
    cfg = _STRATEGY_WINNERS.get(profile, {})
    if not isinstance(cfg, dict):
        return default
    nested = cfg.get(section, {})
    if not isinstance(nested, dict):
        return default
    return nested.get(key, default)


def _optional_signal_kwargs(profile: str) -> dict:
    """전략별 선택 파라미터를 winner 설정에서 MarketConfig 키로 변환한다."""
    return {
        "ema_fast": int(_winner_value(profile, "ema_fast", 5)),
        "ema_slow": int(_winner_value(profile, "ema_slow", 20)),
        "slope_lookback": int(_winner_value(profile, "slope_lookback", 1)),
        "rsi_buy_threshold": int(_winner_value(profile, "rsi_buy_threshold", 30)),
        "bb_std": float(_winner_value(profile, "bb_std", 2.0)),
        "vwap_window": int(_winner_value(profile, "vwap_window", 96)),
        "vwap_pullback_lookback": int(_winner_value(profile, "vwap_pullback_lookback", 5)),
        "vwap_volume_mult": float(_winner_value(profile, "vwap_volume_mult", 1.0)),
    }


@dataclass(frozen=True)
class MarketConfig:
    market: str           # "KRW-XRP"
    asset: str            # "XRP"
    label: str            # 화면 표시용
    state_file: Path
    # 진입 시그널
    entry_kind: str       # volatility_breakout | trend_pullback | mean_reversion | vwap_pullback | combo_or
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
    ema_fast: int = 5
    ema_slow: int = 20
    slope_lookback: int = 1
    rsi_buy_threshold: int = 30
    bb_std: float = 2.0
    vwap_window: int = 96
    vwap_pullback_lookback: int = 5
    vwap_volume_mult: float = 1.0


XRP_CFG = MarketConfig(
    market="KRW-XRP", asset="XRP", label=_winner_value("KRW-XRP", "label", "XRP v3"),
    state_file=STATE_DIR / "xrp_night_state.json",
    entry_kind=_winner_value("KRW-XRP", "entry_kind", "volatility_breakout"),
    vol_mult=float(_winner_value("KRW-XRP", "breakout_volume_mult", 1.5)),
    atr_mult=float(_winner_value("KRW-XRP", "atr_breakout_mult", 1.5)),
    atr_window=int(_winner_value("KRW-XRP", "atr_window", 14)),
    vol_baseline=int(_winner_value("KRW-XRP", "volume_baseline_window", 10)),
    lb=int(_winner_value("KRW-XRP", "breakout_lookback", 10)),
    take_profit=float(_winner_value("KRW-XRP", "take_profit", 2.0)),
    stop_loss=float(_winner_value("KRW-XRP", "stop_loss", 1.5)),
    max_hold_bars=int(_winner_value("KRW-XRP", "max_hold_bars", 60)),
    tp1_pct=float(_winner_value("KRW-XRP", "tp1_pct", 1.2)),
    tp1_ratio=float(_winner_value("KRW-XRP", "tp1_ratio", 0.7)),
    session=_winner_value("KRW-XRP", "session", "kr_night"),
    weekday=_winner_value("KRW-XRP", "weekday", "weekday"),
    **_optional_signal_kwargs("KRW-XRP"),
)

ETH_CFG = MarketConfig(
    market="KRW-ETH", asset="ETH", label=_winner_value("KRW-ETH", "label", "ETH F1"),
    state_file=STATE_DIR / "eth_state.json",
    entry_kind=_winner_value("KRW-ETH", "entry_kind", "volatility_breakout"),
    vol_mult=float(_winner_value("KRW-ETH", "breakout_volume_mult", 2.5)),
    atr_mult=float(_winner_value("KRW-ETH", "atr_breakout_mult", 1.0)),
    atr_window=int(_winner_value("KRW-ETH", "atr_window", 14)),
    vol_baseline=int(_winner_value("KRW-ETH", "volume_baseline_window", 10)),
    lb=int(_winner_value("KRW-ETH", "breakout_lookback", 10)),
    take_profit=float(_winner_value("KRW-ETH", "take_profit", 2.0)),
    stop_loss=float(_winner_value("KRW-ETH", "stop_loss", 1.5)),
    max_hold_bars=int(_winner_value("KRW-ETH", "max_hold_bars", 60)),
    tp1_pct=float(_winner_value("KRW-ETH", "tp1_pct", 0.0)),
    tp1_ratio=float(_winner_value("KRW-ETH", "tp1_ratio", 0.5)),
    session=_winner_value("KRW-ETH", "session", "kr_night"),
    weekday=_winner_value("KRW-ETH", "weekday", "all"),
    **_optional_signal_kwargs("KRW-ETH"),
)

BTC_CFG = MarketConfig(
    market="KRW-BTC", asset="BTC", label=_winner_value("KRW-BTC", "label", "BTC W1"),
    state_file=STATE_DIR / "btc_state.json",
    entry_kind=_winner_value("KRW-BTC", "entry_kind", "volatility_breakout"),
    vol_mult=float(_winner_value("KRW-BTC", "breakout_volume_mult", 2.5)),
    atr_mult=float(_winner_value("KRW-BTC", "atr_breakout_mult", 2.0)),
    atr_window=int(_winner_value("KRW-BTC", "atr_window", 14)),
    vol_baseline=int(_winner_value("KRW-BTC", "volume_baseline_window", 10)),
    lb=int(_winner_value("KRW-BTC", "breakout_lookback", 10)),
    take_profit=float(_winner_value("KRW-BTC", "take_profit", 3.0)),
    stop_loss=float(_winner_value("KRW-BTC", "stop_loss", 2.0)),
    max_hold_bars=int(_winner_value("KRW-BTC", "max_hold_bars", 48)),
    tp1_pct=float(_winner_value("KRW-BTC", "tp1_pct", 0.0)),
    tp1_ratio=float(_winner_value("KRW-BTC", "tp1_ratio", 0.5)),
    session=_winner_value("KRW-BTC", "session", "kr_day"),
    weekday=_winner_value("KRW-BTC", "weekday", "all"),
    **_optional_signal_kwargs("KRW-BTC"),
)

MARKETS: list[MarketConfig] = [XRP_CFG, ETH_CFG, BTC_CFG]
FIXED_MARKET_CODES = {cfg.market for cfg in MARKETS}
MIN_ORDER_KRW = 5_000
MAX_CONCURRENT_POSITIONS = 3
UPBIT_TICKER_ALL_URL = "https://api.upbit.com/v1/ticker/all"
UPBIT_CANDLE_5M_URL = "https://api.upbit.com/v1/candles/minutes/5"
TOP_GAINER_STATE_FILE = STATE_DIR / "top_gainer_state.json"
TOP_GAINER_2_STATE_FILE = STATE_DIR / "top_gainer_2_state.json"
TOP_GAINER_COOLDOWN_FILE = STATE_DIR / "top_gainer_cooldowns.json"
TOP_GAINER_STATE_FILES = {
    1: TOP_GAINER_STATE_FILE,
    2: TOP_GAINER_2_STATE_FILE,
}
TOP_GAINER_LABEL = "TOP_GAINER"
TOP_GAINER_SLOT_COUNT = 2
TOP_GAINER_MIN_CANDLES = 200
TOP_GAINER_CANDIDATE_CHECK_LIMIT = 40
TOP_GAINER_MIN_ACC_TRADE_PRICE_24H = int(_winner_nested_value(
    "TOP_GAINER", "selection_filters", "min_acc_trade_price_24h", 1_000_000_000,
))
TOP_GAINER_MAX_SPREAD_PCT = float(_winner_nested_value(
    "TOP_GAINER", "selection_filters", "max_orderbook_spread_pct", 0.35,
))
TOP_GAINER_STOP_LOSS_COOLDOWN_MINUTES = int(_winner_nested_value(
    "TOP_GAINER", "selection_filters", "stop_loss_cooldown_minutes", 30,
))
UPBIT_ORDERBOOK_URL = "https://api.upbit.com/v1/orderbook"
TOP_GAINER_CFG_TEMPLATE = {
    "entry_kind": _winner_value("TOP_GAINER", "entry_kind", "volatility_breakout"),
    "vol_mult": float(_winner_value("TOP_GAINER", "breakout_volume_mult", 2.5)),
    "atr_mult": float(_winner_value("TOP_GAINER", "atr_breakout_mult", 1.0)),
    "atr_window": int(_winner_value("TOP_GAINER", "atr_window", 14)),
    "vol_baseline": int(_winner_value("TOP_GAINER", "volume_baseline_window", 10)),
    "lb": int(_winner_value("TOP_GAINER", "breakout_lookback", 10)),
    "take_profit": float(_winner_value("TOP_GAINER", "take_profit", 2.0)),
    "stop_loss": float(_winner_value("TOP_GAINER", "stop_loss", 1.5)),
    "max_hold_bars": int(_winner_value("TOP_GAINER", "max_hold_bars", 60)),
    "tp1_pct": float(_winner_value("TOP_GAINER", "tp1_pct", 0.0)),
    "tp1_ratio": float(_winner_value("TOP_GAINER", "tp1_ratio", 0.5)),
    "session": _winner_value("TOP_GAINER", "session", "all"),
    "weekday": _winner_value("TOP_GAINER", "weekday", "all"),
    **_optional_signal_kwargs("TOP_GAINER"),
}

_last_status_hour: int = -1
_last_dynamic_selection_period: str = ""
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


def _load_top_gainer_cooldowns() -> dict[str, str]:
    try:
        raw = json.loads(TOP_GAINER_COOLDOWN_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_top_gainer_cooldowns(cooldowns: dict[str, str]) -> None:
    TOP_GAINER_COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOP_GAINER_COOLDOWN_FILE.write_text(
        json.dumps(cooldowns, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _cooldown_until(market: str, now: datetime) -> Optional[datetime]:
    value = _load_top_gainer_cooldowns().get(market)
    if not value:
        return None
    try:
        until = datetime.fromisoformat(value)
    except ValueError:
        return None
    return until if until > now else None


def _set_top_gainer_cooldown(market: str, now: datetime) -> datetime:
    """동적 슬롯 손절 후 같은 마켓 재진입을 일시 차단한다."""
    cooldowns = _load_top_gainer_cooldowns()
    until = now + timedelta(minutes=TOP_GAINER_STOP_LOSS_COOLDOWN_MINUTES)
    cooldowns[market] = until.isoformat()

    # 만료된 항목은 저장 시 정리해 쿨다운 파일이 계속 커지지 않게 한다.
    cleaned: dict[str, str] = {}
    for key, value in cooldowns.items():
        try:
            if datetime.fromisoformat(value) > now:
                cleaned[key] = value
        except ValueError:
            continue
    _save_top_gainer_cooldowns(cleaned)
    return until


def _dynamic_selection_period(now: datetime) -> str:
    """동적 TOP 선정 주기를 1분 단위로 고정한다."""
    return now.strftime("%Y%m%d%H%M")


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


def _orderbook_spread_pct(market: str, timeout_s: float = 3.0) -> Optional[float]:
    """최우선 호가 기준 스프레드 비율을 계산한다."""
    try:
        res = requests.get(
            UPBIT_ORDERBOOK_URL,
            params={"markets": market},
            headers={"Accept": "application/json"},
            timeout=timeout_s,
        )
        res.raise_for_status()
        rows = res.json()
        if not isinstance(rows, list) or not rows:
            return None
        units = rows[0].get("orderbook_units", [])
        if not units:
            return None
        ask = float(units[0].get("ask_price", 0))
        bid = float(units[0].get("bid_price", 0))
        if ask <= 0 or bid <= 0:
            return None
        mid = (ask + bid) / 2.0
        return (ask - bid) / mid * 100.0 if mid > 0 else None
    except Exception as e:
        logger.warning(f"[{TOP_GAINER_LABEL}] {market} 호가 스프레드 확인 실패: {e}")
        return None


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
        acc_trade_price_24h = row.get("acc_trade_price_24h")
        if not market.startswith("KRW-"):
            continue
        if market in excluded:
            continue
        if row.get("market_warning", "NONE") != "NONE":
            continue
        if bool(row.get("is_trading_suspended", False)):
            continue
        if signed_change_rate is None or trade_price is None or acc_trade_price_24h is None:
            continue
        try:
            rate = float(signed_change_rate)
            price = float(trade_price)
            trade_amount_24h = float(acc_trade_price_24h)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        if trade_amount_24h < TOP_GAINER_MIN_ACC_TRADE_PRICE_24H:
            logger.info(
                f"[{TOP_GAINER_LABEL}] {market} 제외: "
                f"24h 거래대금 {trade_amount_24h:,.0f}원 < {TOP_GAINER_MIN_ACC_TRADE_PRICE_24H:,}원"
            )
            continue
        candidates.append({
            "market": market,
            "signed_change_rate": rate,
            "trade_price": price,
            "acc_trade_price_24h": trade_amount_24h,
        })

    if not candidates:
        raise ValueError("선정 가능한 KRW 상승률 후보가 없습니다.")

    selected: list[dict] = []
    selected_markets: set[str] = set()
    candidates.sort(key=lambda item: item["signed_change_rate"], reverse=True)
    for item in candidates[:TOP_GAINER_CANDIDATE_CHECK_LIMIT]:
        # 상승률 상위권 신규 상장 코인은 캔들 수가 부족해 전략 계산이 불가능할 수 있다.
        if item["market"] in selected_markets:
            continue
        cooldown_until = _cooldown_until(item["market"], datetime.now())
        if cooldown_until is not None:
            logger.info(
                f"[{TOP_GAINER_LABEL}] {item['market']} 제외: "
                f"손절 쿨다운 {cooldown_until.strftime('%H:%M:%S')}까지"
            )
            continue
        spread_pct = _orderbook_spread_pct(item["market"], timeout_s=timeout_s)
        if spread_pct is None:
            logger.info(f"[{TOP_GAINER_LABEL}] {item['market']} 제외: 호가 스프레드 확인 불가")
            continue
        if spread_pct > TOP_GAINER_MAX_SPREAD_PCT:
            logger.info(
                f"[{TOP_GAINER_LABEL}] {item['market']} 제외: "
                f"스프레드 {spread_pct:.3f}% > {TOP_GAINER_MAX_SPREAD_PCT:.3f}%"
            )
            continue
        item["spread_pct"] = spread_pct
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
    """매분 TOP 상승률 2개 종목을 갱신하되, 보유 중인 슬롯은 기존 포지션을 유지한다."""
    global _last_dynamic_selection_period, _dynamic_selected_markets, _dynamic_top_gainer_meta

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

    selection_period = _dynamic_selection_period(now)
    if selection_period == _last_dynamic_selection_period:
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
        _last_dynamic_selection_period = selection_period
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
                f"rate={top['signed_change_rate'] * 100:+.2f}% price={top['trade_price']:.8f} "
                f"amount24h={top.get('acc_trade_price_24h', 0):.0f} spread={top.get('spread_pct', 0):.3f}%"
            )
            active.append(_build_top_gainer_cfg(top["market"], top["signed_change_rate"], slot=slot))
        return active
    except Exception as e:
        logger.warning(f"[{TOP_GAINER_LABEL}] 상승률 TOP2 선정 실패: {e}")
        _last_dynamic_selection_period = selection_period
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

def _signal_params_from_cfg(cfg: MarketConfig) -> LiveSignalParams:
    """MarketConfig를 라이브 시그널 계산 파라미터로 변환한다."""
    return LiveSignalParams(
        entry_kind=cfg.entry_kind,
        ema_fast=cfg.ema_fast,
        ema_slow=cfg.ema_slow,
        slope_lookback=cfg.slope_lookback,
        rsi_buy_threshold=cfg.rsi_buy_threshold,
        bb_std=cfg.bb_std,
        breakout_volume_mult=cfg.vol_mult,
        atr_breakout_mult=cfg.atr_mult,
        atr_window=cfg.atr_window,
        volume_baseline_window=cfg.vol_baseline,
        breakout_lookback=cfg.lb,
        vwap_window=cfg.vwap_window,
        vwap_pullback_lookback=cfg.vwap_pullback_lookback,
        vwap_volume_mult=cfg.vwap_volume_mult,
    )


def compute_indicators(df: pd.DataFrame, cfg: MarketConfig) -> dict:
    """분봉 DataFrame(오름차순)에서 마지막 완성 캔들(iloc[-2]) 기준 시그널."""
    return evaluate_live_signal(df, _signal_params_from_cfg(cfg))


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


def _active_position_count(account: dict) -> int:
    """최소주문 금액 이상으로 보유 중인 코인 포지션 수를 계산한다."""
    count = 0
    for asset, row in account.items():
        if asset in {"KRW", "krw_balance", "krw_available"}:
            continue
        if not isinstance(row, dict):
            continue
        try:
            balance = float(row.get("balance", 0))
            avg_price = float(row.get("avg_buy_price", 0))
        except (TypeError, ValueError):
            continue
        if balance > 0 and avg_price > 0 and balance * avg_price >= MIN_ORDER_KRW:
            count += 1
    return count


def _buy_budget_krw(account: dict) -> tuple[int, int, int]:
    """
    최대 동시 3종목 운용을 위한 1회 매수 금액을 계산한다.

    남은 슬롯 수로 현재 가용 KRW를 나누므로 0개 보유 시 1/3,
    1개 보유 시 1/2, 2개 보유 시 남은 금액 전체를 사용한다.
    """
    active_count = _active_position_count(account)
    remaining_slots = MAX_CONCURRENT_POSITIONS - active_count
    if remaining_slots <= 0:
        return 0, active_count, 0
    krw_available = int(account.get("krw_available", 0))
    return math.floor(krw_available / remaining_slots), active_count, remaining_slots


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


def _mark_dust_position(cfg: MarketConfig, state: dict, price: float, now: datetime) -> dict:
    """최소주문 미만 잔여 포지션은 반복 매도 실패를 막기 위해 별도 상태로 남긴다."""
    dust_state = {
        **_default_state(),
        "dust_position": True,
        "dust_market": cfg.market,
        "dust_asset": cfg.asset,
        "dust_price": price,
        "dust_marked_at": now.isoformat(),
        "last_entry_signal_candle": state.get("last_entry_signal_candle"),
    }
    if _is_dynamic_cfg(cfg):
        slot = _dynamic_slot(cfg) or 1
        meta = _dynamic_top_gainer_meta.get(slot, {})
        dust_state.update({
            "market": cfg.market,
            "asset": cfg.asset,
            "slot": slot,
            "signed_change_rate": state.get("signed_change_rate", meta.get("signed_change_rate")),
            "selected_trade_price": state.get("selected_trade_price", meta.get("trade_price")),
            "selected_at": state.get("selected_at", meta.get("selected_at")),
        })
    save_state(cfg, dust_state)
    logger.info(f"[{cfg.market}] 최소주문 미만 잔여 포지션 dust 처리 price={price:.5f}")
    return dust_state


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


def _fmt_entry_signal(cfg: MarketConfig, indic: dict) -> str:
    """현재 진입 전략에 맞는 핵심 시그널 상태를 짧게 표시한다."""
    kind = indic.get("entry_kind", cfg.entry_kind)
    if kind == "volatility_breakout":
        price_ok = "Y" if indic["close"] > indic["bt"] else "N"
        vol_ok = "Y" if indic["vol_ratio"] >= cfg.vol_mult else "N"
        return (
            f"vol_breakout price={price_ok} vol={vol_ok} "
            f"({indic['vol_ratio']:.2f}x/{cfg.vol_mult}x)"
        )
    if kind == "trend_pullback":
        return (
            f"trend_pullback signal={'Y' if indic['trend_signal'] else 'N'} "
            f"EMA{cfg.ema_fast}/{cfg.ema_slow}"
        )
    if kind == "mean_reversion":
        return (
            f"mean_reversion signal={'Y' if indic['mean_reversion_signal'] else 'N'} "
            f"RSI={indic['rsi']:.1f}/{cfg.rsi_buy_threshold}"
        )
    if kind == "vwap_pullback":
        return (
            f"vwap_pullback signal={'Y' if indic['vwap_signal'] else 'N'} "
            f"vol={indic['vol_ratio']:.2f}x/{cfg.vwap_volume_mult}x"
        )
    if kind == "combo_or":
        return (
            "combo_or "
            f"trend={'Y' if indic['trend_signal'] else 'N'} "
            f"mean={'Y' if indic['mean_reversion_signal'] else 'N'} "
            f"vol={'Y' if indic['volatility_signal'] else 'N'}"
        )
    return f"{kind} signal={'Y' if indic.get('entry_signal') else 'N'}"


# ── 텔레그램 메시지: 매매 알림 ─────────────────────────────────────────────

def _msg_buy(cfg: MarketConfig, now: datetime, buy_price: float,
             krw_used: int, qty: float, krw_remaining: float) -> str:
    lines = [
        f"🟢 매수 체결 — {cfg.market} ({cfg.label})",
        f"시각: {_ts_short(now)}",
        f"매수가: {_fmt_price(buy_price)}",
        f"수량: {qty:.4f} {cfg.asset}",
        f"투자금: {krw_used:,}원 (최대 {MAX_CONCURRENT_POSITIONS}종목 슬롯 배분)",
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

def _position_summary_line(cfg: MarketConfig, account: dict, state: dict,
                           cur_price: float, now: datetime) -> tuple[float, str]:
    if not has_position(account, cfg, cur_price):
        return 0.0, ""

    balance = float(account[cfg.asset]["balance"])
    buy_price = state.get("buy_price") or account[cfg.asset]["avg_buy_price"]
    value = balance * cur_price
    pnl_pct = (cur_price / buy_price - 1.0) * 100.0 if buy_price > 0 else 0.0
    hold_bars = hold_bars_elapsed(state.get("buy_time"), now)
    return value, (
        f"- {cfg.market}: {_fmt_price(value)} "
        f"PnL {pnl_pct:+.2f}% / 보유 {_fmt_hold(hold_bars)}"
    )


def _fmt_hourly_event(cfg: MarketConfig, event: dict) -> str:
    tm = event.get("time", "--:--")
    kind = event.get("kind", "")
    if kind == "BUY":
        return f"{tm} {cfg.market} 매수 @{_fmt_price(event.get('price', 0))}"
    if kind == "BUY_FAIL":
        return f"{tm} {cfg.market} 매수실패 {event.get('detail', '')[:40]}"
    if kind == "BUY_BLOCKED":
        return f"{tm} {cfg.market} 매수보류 {event.get('detail', '')[:40]}"
    if kind == "SELL_SL":
        return (
            f"{tm} {cfg.market} 손절 @{_fmt_price(event.get('price', 0))} "
            f"{event.get('pnl_pct', 0):+.2f}%"
        )
    if kind == "SELL_TP":
        return (
            f"{tm} {cfg.market} 익절 @{_fmt_price(event.get('price', 0))} "
            f"{event.get('pnl_pct', 0):+.2f}%"
        )
    if kind == "SELL_TP1":
        return (
            f"{tm} {cfg.market} TP1 @{_fmt_price(event.get('price', 0))} "
            f"{event.get('pnl_pct', 0):+.2f}%"
        )
    if kind == "SELL_TIME":
        return (
            f"{tm} {cfg.market} 시간청산 @{_fmt_price(event.get('price', 0))} "
            f"{event.get('pnl_pct', 0):+.2f}%"
        )
    if kind == "SELL_FAIL":
        return f"{tm} {cfg.market} 매도실패 {event.get('detail', '')[:40]}"
    if kind == "DUST":
        return f"{tm} {cfg.market} 소액잔여 {event.get('value_krw', 0):,.0f}원"
    return f"{tm} {cfg.market} {kind}"


def _hourly_simple_activity(configs: list[MarketConfig]) -> tuple[str, list[str]]:
    events: list[tuple[MarketConfig, dict]] = [
        (cfg, event)
        for cfg in configs
        for event in _hourly_events.get(cfg.market, [])
    ]
    total = len(events)
    trades = [
        (cfg, event)
        for cfg, event in events
        if event.get("kind") in {
            "BUY", "BUY_FAIL", "BUY_BLOCKED",
            "SELL_SL", "SELL_TP", "SELL_TP1", "SELL_TIME", "SELL_FAIL",
            "DUST",
        }
    ]
    no_signal = sum(1 for _, event in events if event.get("kind") == "NO_SIGNAL")
    out_of_session = sum(1 for _, event in events if event.get("kind") == "OUT_OF_SESSION")
    holds = sum(1 for _, event in events if event.get("kind") == "HOLD")
    summary = (
        f"판단 {total}회 / 거래·시도 {len(trades)}회 / "
        f"무신호 {no_signal}회 / 보유유지 {holds}회 / 세션외 {out_of_session}회"
    )
    logs = [_fmt_hourly_event(cfg, event) for cfg, event in trades[-12:]]
    return summary, logs


def _combined_status_msg(now: datetime, snapshots: dict, krw_balance: float,
                         configs: Optional[list[MarketConfig]] = None) -> str:
    lines = [f"📊 시간별 요약 — {_ts_full(now)}", ""]
    active_configs = configs or MARKETS
    active_dynamic_slots = {
        slot for cfg in active_configs
        if (slot := _dynamic_slot(cfg)) is not None
    }
    asset_value_total = 0.0
    position_lines: list[str] = []
    for cfg in active_configs:
        snap = snapshots.get(cfg.market)
        if snap is None:
            continue
        account, state, cur_price, indic = snap
        value, line = _position_summary_line(cfg, account, state, cur_price, now)
        asset_value_total += value
        if line:
            position_lines.append(line)

    lines.append("[전체 자산]")
    lines.append(f"총평가: {asset_value_total + krw_balance:,.0f}원")
    lines.append(f"KRW: {krw_balance:,.0f}원 / 코인: {asset_value_total:,.0f}원")
    lines.append(f"보유: {len(position_lines)}개")
    if position_lines:
        lines.extend(position_lines[:6])
        if len(position_lines) > 6:
            lines.append(f"- 외 {len(position_lines) - 6}개")
    else:
        lines.append("- 보유 포지션 없음")
    lines.append("")

    lines.append("[지난 1시간 판단/매매]")
    summary, trade_logs = _hourly_simple_activity(active_configs)
    lines.append(summary)
    if trade_logs:
        lines.extend(f"- {log}" for log in trade_logs)
    else:
        lines.append("- 매매/주문시도 없음")

    failed = [
        (slot, meta) for slot, meta in _dynamic_top_gainer_meta.items()
        if meta.get("status") == "failed" and slot not in active_dynamic_slots
    ]
    if failed:
        lines.append("")
        lines.append("[TOP 선정 실패]")
        for slot, meta in failed:
            lines.append(f"- TOP{slot}: {meta.get('last_error', '')[:80]}")
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
            f"  시그널: {cfg.entry_kind}, vol×{cfg.vol_mult}, atr×{cfg.atr_mult}, lb={cfg.lb}, vbase={cfg.vol_baseline}",
            f"  청산: TP={cfg.take_profit}%, SL={cfg.stop_loss}%, 보유한도={cfg.max_hold_bars}봉 ({cfg.max_hold_bars*5/60:.0f}h)",
            tp1_str,
            f"  매수한도: 최대 {MAX_CONCURRENT_POSITIONS}종목 슬롯 배분",
            "",
        ])
    parts.append(f"총 매수한도: 최대 {MAX_CONCURRENT_POSITIONS}종목 동시 보유 기준 슬롯 배분")
    parts.extend([
        "",
        "━ 동적 슬롯 (TOP_GAINER ×2) ━",
        "  대상: KRW 마켓 전일대비 상승률 TOP 2 (고정 3개 시장 제외)",
        "  갱신: 매분 재선정",
        "  보유정책: 슬롯별 보유 중 강제 교체 없음 (TP/SL/시간청산까지 유지)",
        f"  전략: {TOP_GAINER_CFG_TEMPLATE['entry_kind']} "
        f"(vol×{TOP_GAINER_CFG_TEMPLATE['vol_mult']}, atr×{TOP_GAINER_CFG_TEMPLATE['atr_mult']}, "
        f"TP={TOP_GAINER_CFG_TEMPLATE['take_profit']}%, SL={TOP_GAINER_CFG_TEMPLATE['stop_loss']}%)",
    ])
    return "\n".join(parts)


def _shutdown_msg(now: datetime) -> str:
    return f"🛑 Multi-Market 봇 종료\n종료: {_ts_full(now)}"


# ── 시장별 트레이딩 ────────────────────────────────────────────────────────

def _normalize_minute_candles(rows: list[dict]) -> pd.DataFrame:
    """업비트 분봉 응답을 라이브 전략에서 쓰는 컬럼 형태로 변환한다."""
    if not rows:
        raise ValueError("캔들정보가 비어 있습니다.")

    df = pd.DataFrame(rows)
    required = {
        "candle_date_time_utc",
        "candle_date_time_kst",
        "opening_price",
        "trade_price",
        "high_price",
        "low_price",
        "candle_acc_trade_volume",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"캔들 응답 필수 컬럼 누락: {sorted(missing)}")

    df["date"] = df.candle_date_time_kst.str.split("T").str[0]
    df["time"] = df.candle_date_time_kst.str.split("T").str[1]
    df["open"] = df["opening_price"]
    df["close"] = df["trade_price"]
    df["high"] = df["high_price"]
    df["low"] = df["low_price"]
    df["volume"] = df["candle_acc_trade_volume"]
    df.drop(
        ["opening_price", "trade_price", "high_price", "low_price", "candle_acc_trade_volume"],
        axis=1,
        inplace=True,
    )
    df.sort_values(by="candle_date_time_kst", inplace=True)
    df.drop_duplicates(subset=["candle_date_time_kst"], keep="last", inplace=True)
    return df


def _fetch_live_min_candles(market: str, minute: int = 5, count: int = 200,
                            timeout_s: float = 5.0) -> pd.DataFrame:
    """
    라이브 매매용 최근 분봉 조회.

    기존 공용 get_min_candle_data는 시장 1개당 API 5회로 1000봉을 가져온다.
    라이브 전략은 최대 lookback/hold 계산에 200봉이면 충분하므로 1회 호출로 제한해
    동적 TOP2 운용 시 rate limit 여유를 확보한다.
    """
    res = requests.get(
        f"https://api.upbit.com/v1/candles/minutes/{minute}",
        params={"market": market, "count": count},
        headers={"Accept": "application/json"},
        timeout=timeout_s,
    )
    res.raise_for_status()
    rows = res.json()
    if isinstance(rows, dict):
        raise ValueError(f"캔들 API 오류 응답: {rows}")
    if not isinstance(rows, list):
        raise ValueError(f"캔들 API 응답 형식 오류: {type(rows).__name__}")
    return _normalize_minute_candles(rows)


def _fetch_candles_with_retry(market: str, attempts: int = 3, sleep_s: float = 1.0) -> pd.DataFrame:
    """라이브 캔들 조회 재시도. 멀티 마켓 호출 시 rate limit 안정성 위함."""
    last_err: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return _fetch_live_min_candles(market, 5)
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

    time_str = now.strftime("%H:%M")

    pos = has_position(account, cfg, cur_price)
    logger.info(
        f"[{cfg.market}] pos={'LONG' if pos else 'NONE'}  close={cur_price:.5f}  "
        f"entry_kind={cfg.entry_kind}  signal={indic['entry_signal']}  "
        f"{_fmt_entry_signal(cfg, indic)}"
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
                if _is_dynamic_cfg(cfg):
                    cooldown_until = _set_top_gainer_cooldown(cfg.market, now)
                    logger.info(
                        f"[{cfg.market}] TOP_GAINER 손절 쿨다운 설정 until={cooldown_until.isoformat()}"
                    )
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
                    f"[{cfg.market}] [SELL] TP1 {cfg.tp1_ratio*100:.0f}%  "
                    f"price={cur_price:.5f}  buy={buy_price:.5f}  qty={sell_qty_str}"
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
            logger.info(
                f"[{cfg.market}] [SELL] 시간청산  price={cur_price:.5f}  buy={buy_price:.5f}  "
                f"hold_bars={hold_bars}/{cfg.max_hold_bars}"
            )
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
        dust_value = _position_value_krw(account, cfg, cur_price)
        if 0 < dust_value < MIN_ORDER_KRW:
            state = _mark_dust_position(cfg, state, cur_price, now)
            _record_event(cfg.market, "DUST", time_str, price=cur_price, value_krw=dust_value)
            return account, state, cur_price, indic
        logger.info(f"[{cfg.market}] 포지션없음 + 상태파일 잔존 → 초기화")
        clear_state(cfg)
        state = _default_state()

    if not passes_filter(now, cfg):
        logger.debug(f"[{cfg.market}] 세션/요일 필터 미통과")
        _record_event(cfg.market, "OUT_OF_SESSION", time_str)
        return account, state, cur_price, indic

    entry = bool(indic["entry_signal"])
    logger.info(
        f"[{cfg.market}] 진입신호={entry}  "
        f"({_fmt_entry_signal(cfg, indic)})"
    )
    if not entry:
        bt_gap_pct = (indic["bt"] / cur_price - 1) * 100 if cur_price > 0 else 0.0
        _record_event(cfg.market, "NO_SIGNAL", time_str,
                      price=cur_price, vol_ratio=indic["vol_ratio"],
                      bt_gap_pct=bt_gap_pct, entry_kind=cfg.entry_kind)
        return account, state, cur_price, indic

    signal_candle = indic.get("candle_time")
    if signal_candle and state.get("last_entry_signal_candle") == signal_candle:
        logger.info(f"[{cfg.market}] 동일 5분봉 진입신호 이미 처리됨: {signal_candle}")
        _record_event(cfg.market, "NO_SIGNAL", time_str,
                      price=cur_price, vol_ratio=indic["vol_ratio"], bt_gap_pct=0.0)
        return account, state, cur_price, indic

    # 매매 판단은 매분 수행하지만, 시그널 자체는 완성된 5분봉 기준이다.
    # 같은 5분봉 신호로 매수 실패/보류 알림이나 재진입이 반복되지 않도록 처리 시각을 저장한다.
    state["last_entry_signal_candle"] = signal_candle
    save_state(cfg, state)

    # 매수 가능 KRW 결정: 최대 3개 동시 보유를 위해 남은 슬롯 수로 가용 KRW를 배분한다.
    krw_use, active_positions, remaining_slots = _buy_budget_krw(account)
    if remaining_slots <= 0:
        msg = f"동시 보유 한도 도달({active_positions}/{MAX_CONCURRENT_POSITIONS})"
        logger.warning(f"[{cfg.market}] {msg}")
        _record_event(cfg.market, "BUY_BLOCKED", time_str, detail=msg)
        return account, state, cur_price, indic

    if krw_use < MIN_ORDER_KRW:
        msg = (
            f"슬롯 배분 매수금액({krw_use:,}원) < 최소주문({MIN_ORDER_KRW:,}원) "
            f"(보유 {active_positions}/{MAX_CONCURRENT_POSITIONS})"
        )
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
            "last_entry_signal_candle": signal_candle,
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
        logger.info(
            f"[{cfg.market}] [BUY] {krw_use:,}원 매수 완료  avg_buy_price={actual_buy:.5f}  "
            f"slots_before={active_positions}/{MAX_CONCURRENT_POSITIONS} remaining={remaining_slots}"
        )
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
            f"  {cfg.market} ({cfg.label}): entry={cfg.entry_kind} vol={cfg.vol_mult} atr={cfg.atr_mult} "
            f"vbase={cfg.vol_baseline} lb={cfg.lb}  "
            f"hold={cfg.max_hold_bars}  TP={cfg.take_profit}% SL={cfg.stop_loss}%  "
            f"tp1={cfg.tp1_pct}/{cfg.tp1_ratio}  ses={cfg.session}/{cfg.weekday}  "
            f"max_positions={MAX_CONCURRENT_POSITIONS}"
        )
    logger.info(
        "  TOP_GAINER x2: KRW ticker/all signed_change_rate TOP2, 1m refresh, "
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
