from __future__ import annotations

import json
import math
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pyupbit
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATE_DIR = BASE_DIR / "data_cache"
LOG_FILE = BASE_DIR / "logs" / "my_log.log"
SUMMARY_DISPLAY_FILE = STATE_DIR / "dashboard_summary_display.json"
PRICE_CACHE: dict[str, float] = {}

load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class DashboardMarket:
    market: str
    asset: str
    label: str
    state_file: str
    take_profit: float
    stop_loss: float
    max_hold_bars: int
    session: str
    slot_type: str


MARKETS: tuple[DashboardMarket, ...] = (
    DashboardMarket("KRW-XRP", "XRP", "XRP v3", "xrp_night_state.json", 2.0, 1.5, 60, "평일 00-09시", "fixed"),
    DashboardMarket("KRW-ETH", "ETH", "ETH freq-balanced", "eth_state.json", 2.0, 1.5, 60, "평일 00-09시", "fixed"),
    DashboardMarket("KRW-BTC", "BTC", "BTC W1", "btc_state.json", 3.0, 2.0, 48, "매일 09-18시", "fixed"),
    DashboardMarket("TOP1", "TOP1", "TOP Gainer 1", "top_gainer_state.json", 2.5, 1.0, 36, "24시간", "dynamic"),
    DashboardMarket("TOP2", "TOP2", "TOP Gainer 2", "top_gainer_2_state.json", 2.5, 1.0, 36, "24시간", "dynamic"),
)

TRADE_RE = re.compile(
    r"(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*"
    r"\[(?P<market>KRW-[A-Z0-9]+)\].*"
    r"\[(?P<action>BUY|SELL)\](?P<message>.*)"
)
TOP_SELECTION_RE = re.compile(
    r"(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*"
    r"\[TOP_GAINER(?P<slot>[12])\] selected (?P<market>KRW-[A-Z0-9]+) "
    r"rate=(?P<rate>[+-]\d+\.\d+)% price=(?P<price>\d+(?:\.\d+)?)"
)
BUY_LOG_DETAIL_RE = re.compile(
    r"(?P<funds>[\d,]+)원\s+매수 완료\s+avg_buy_price=(?P<avg_price>\d+(?:\.\d+)?)"
)
SELL_LOG_DETAIL_RE = re.compile(
    r"(?P<reason>.*?)\s+price=(?P<avg_price>\d+(?:\.\d+)?)\s+buy=(?P<buy_price>\d+(?:\.\d+)?)"
)
FAIL_LOG_RE = re.compile(
    r"(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*"
    r"\[(?P<market>KRW-[A-Z0-9]+)\]\s+"
    r"(?P<action>매수|손절|익절|시간청산|TP1)\s+실패:"
)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def _display_summary(live_summary: dict[str, float], interval_seconds: int = 60) -> dict[str, float]:
    cached = _read_json(SUMMARY_DISPLAY_FILE)
    now = datetime.now(timezone.utc)
    live_total = _float(live_summary.get("total_assets"))
    try:
        cached_at = datetime.fromisoformat(str(cached.get("timestamp", "")))
    except ValueError:
        cached_at = None

    if cached_at is not None and (now - cached_at).total_seconds() < interval_seconds:
        summary = cached.get("summary")
        cached_total = _float(summary.get("total_assets")) if isinstance(summary, dict) else 0.0
        if (
            isinstance(summary, dict)
            and cached_total > 0
            and (live_total <= 0 or 0.5 <= cached_total / live_total <= 2.0)
        ):
            return summary

    payload = {"timestamp": now.isoformat(), "summary": live_summary}
    try:
        SUMMARY_DISPLAY_FILE.parent.mkdir(parents=True, exist_ok=True)
        SUMMARY_DISPLAY_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return live_summary


def _trade_log_paths() -> list[Path]:
    paths = list((BASE_DIR / "logs").glob("my_log.log*"))
    out_file = BASE_DIR / "logs" / "main_multi_market.out"
    if out_file.exists():
        paths.append(out_file)
    return sorted(set(paths), key=lambda item: item.name)


def _read_trade_log_lines() -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for path in _trade_log_paths():
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                # 날짜별 로그와 out 파일에 같은 체결 로그가 중복 저장될 수 있어 원문 기준으로 한 번만 사용한다.
                if line in seen:
                    continue
                seen.add(line)
                lines.append(line)
        except OSError:
            continue
    return sorted(lines)


def _resolve_market(cfg: DashboardMarket) -> tuple[str, str, dict[str, Any]]:
    state = _read_json(STATE_DIR / cfg.state_file)
    if cfg.slot_type == "dynamic":
        if not state.get("market"):
            slot = "1" if cfg.market == "TOP1" else "2"
            state = {**_latest_dynamic_selection().get(slot, {}), **state}
        market = str(state.get("market") or cfg.market)
        asset = str(state.get("asset") or (market.split("-", 1)[1] if "-" in market else cfg.asset))
        return market, asset, state
    return cfg.market, cfg.asset, state


def _get_upbit_client() -> pyupbit.Upbit:
    access_key = os.getenv("ACCESS_KEY", "")
    secret_key = os.getenv("SECRET_KEY", "")
    if not access_key or not secret_key:
        raise RuntimeError("ACCESS_KEY/SECRET_KEY 환경 변수가 없습니다. .env를 확인하세요.")
    return pyupbit.Upbit(access_key, secret_key)


def _normalize_balances(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    assets: dict[str, dict[str, Any]] = {}
    for row in rows:
        currency = str(row.get("currency", ""))
        if not currency:
            continue
        balance = _float(row.get("balance"))
        locked = _float(row.get("locked"))
        avg_buy_price = _float(row.get("avg_buy_price"))
        assets[currency] = {
            "currency": currency,
            "balance": balance,
            "locked": locked,
            "avg_buy_price": avg_buy_price,
            "unit_currency": row.get("unit_currency", "KRW"),
        }
    return assets


def _current_prices(markets: list[str]) -> dict[str, float]:
    valid = sorted({market for market in markets if market.startswith("KRW-")})
    if not valid:
        return {}
    try:
        prices = pyupbit.get_current_price(valid)
        if isinstance(prices, dict):
            fresh = {market: _float(price) for market, price in prices.items() if _float(price) > 0}
            PRICE_CACHE.update(fresh)
            return {market: fresh.get(market, PRICE_CACHE.get(market, 0.0)) for market in valid}
        if len(valid) == 1:
            price = _float(prices)
            if price > 0:
                PRICE_CACHE[valid[0]] = price
            return {valid[0]: PRICE_CACHE.get(valid[0], 0.0)}
    except Exception:
        pass

    prices_by_market: dict[str, float] = {}
    for market in valid:
        try:
            # 보유 자산 중 KRW 마켓이 없는 코인이 섞여도 전체 API가 실패하지 않도록 개별 조회로 격리한다.
            current_price = pyupbit.get_current_price(market)
            if current_price is not None:
                price = _float(current_price)
                if price > 0:
                    PRICE_CACHE[market] = price
                prices_by_market[market] = PRICE_CACHE.get(market, 0.0)
        except Exception:
            prices_by_market[market] = PRICE_CACHE.get(market, 0.0)
    return prices_by_market


def _ohlcv_points(market: str, count: int = 48) -> list[dict[str, Any]]:
    try:
        df = pyupbit.get_ohlcv(market, interval="minute5", count=count)
        if df is None or df.empty:
            return []
        points: list[dict[str, Any]] = []
        for idx, row in df.tail(count).iterrows():
            points.append({
                "time": idx.strftime("%H:%M"),
                "timestamp": idx.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                "open": _float(row.get("open")),
                "high": _float(row.get("high")),
                "low": _float(row.get("low")),
                "close": _float(row.get("close")),
                "volume": _float(row.get("volume")),
            })
        return points
    except Exception:
        return []


def _order_history(client: pyupbit.Upbit, markets: list[str], limit: int) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for market in markets:
        if not market.startswith("KRW-"):
            continue
        try:
            rows = client.get_order(market, state="done", page=1, limit=min(limit, 100))
            if not isinstance(rows, list):
                continue
            for row in rows:
                created_at = str(row.get("created_at", ""))
                executed_volume = _float(row.get("executed_volume"))
                executed_funds = _float(row.get("executed_funds"))
                paid_fee = _float(row.get("paid_fee"))
                if executed_funds <= 0 and paid_fee > 0:
                    # 시장가 매도 목록 응답은 체결금액이 비어 있는 경우가 있어, 업비트 기본 수수료율(0.05%)로 보수적 추정한다.
                    executed_funds = paid_fee / 0.0005
                avg_price = executed_funds / executed_volume if executed_volume > 0 else _float(row.get("price"))
                history.append({
                    "uuid": row.get("uuid", ""),
                    "market": row.get("market", market),
                    "side": row.get("side", ""),
                    "created_at": created_at,
                    "volume": executed_volume,
                    "funds": executed_funds,
                    "avg_price": avg_price,
                    "paid_fee": paid_fee,
                })
        except Exception as exc:
            history.append({"market": market, "error": str(exc)})

    return sorted(history, key=lambda item: str(item.get("created_at", "")), reverse=True)[:limit]


def _log_time_to_iso(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return parsed.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    except ValueError:
        return value


def _log_trade_history(limit: int = 60) -> list[dict[str, Any]]:
    lines = _read_trade_log_lines()
    failed_keys: set[tuple[str, str, str]] = set()
    for line in lines:
        match = FAIL_LOG_RE.search(line)
        if match:
            item = match.groupdict()
            side = "bid" if item["action"] == "매수" else "ask"
            failed_keys.add((item["time"][:16], item["market"], side))

    trades: list[dict[str, Any]] = []
    for line in reversed(lines):
        match = TRADE_RE.search(line)
        if not match:
            continue

        item = match.groupdict()
        if (item["time"][:16], item["market"]) in failed_keys:
            continue
        action = item["action"]
        message = item.get("message", "")
        market = item["market"]
        created_at = _log_time_to_iso(item["time"])

        if action == "BUY":
            side = "bid"
            if (item["time"][:16], market, side) in failed_keys:
                continue
            detail = BUY_LOG_DETAIL_RE.search(message)
            funds = _float(detail.group("funds").replace(",", "")) if detail else 0.0
            avg_price = _float(detail.group("avg_price")) if detail else 0.0
            pnl_pct = 0.0
            reason = ""
        else:
            side = "ask"
            if (item["time"][:16], market, side) in failed_keys:
                continue
            detail = SELL_LOG_DETAIL_RE.search(message)
            funds = 0.0
            avg_price = _float(detail.group("avg_price")) if detail else 0.0
            buy_price = _float(detail.group("buy_price")) if detail else 0.0
            pnl_pct = (avg_price / buy_price - 1.0) * 100.0 if buy_price > 0 else 0.0
            reason = (detail.group("reason").strip() if detail else message.strip().split("  ")[0].strip())

        trades.append({
            "uuid": f"log-{created_at}-{market}-{side}",
            "market": market,
            "side": side,
            "created_at": created_at,
            "volume": 0.0,
            "funds": funds,
            "avg_price": avg_price,
            "paid_fee": 0.0,
            "source": "bot_log",
            "memo": message.strip(),
            "sell_reason": reason,
            "pnl_pct": pnl_pct,
        })
        if len(trades) >= limit:
            break
    return trades


def _enrich_trade_pnl(orders: list[dict[str, Any]], prices: dict[str, float]) -> list[dict[str, Any]]:
    chronological = sorted(
        [order for order in orders if not order.get("error")],
        key=lambda item: str(item.get("created_at", "")),
    )
    buys_by_market: dict[str, list[dict[str, Any]]] = {}
    sells_by_market: dict[str, list[dict[str, Any]]] = {}
    for order in chronological:
        if order.get("side") == "bid":
            buys_by_market.setdefault(str(order.get("market", "")), []).append(order)
        elif order.get("side") == "ask":
            sells_by_market.setdefault(str(order.get("market", "")), []).append(order)

    enriched: list[dict[str, Any]] = []
    for order in orders:
        market = str(order.get("market", ""))
        side = order.get("side")
        if side == "bid":
            buy_time = str(order.get("created_at", ""))
            buy_price = _float(order.get("avg_price"))
            matched_sell = next(
                (
                    sell for sell in sells_by_market.get(market, [])
                    if str(sell.get("created_at", "")) > buy_time and _float(sell.get("avg_price")) > 0
                ),
                None,
            )
            if matched_sell and buy_price > 0:
                sell_price = _float(matched_sell.get("avg_price"))
                matched_pnl_pct = _float(matched_sell.get("pnl_pct"))
                order = {
                    **order,
                    "pnl_pct": matched_pnl_pct if abs(matched_pnl_pct) > 1e-9 else (sell_price / buy_price - 1.0) * 100.0,
                    "pnl_basis": "매도완료",
                    "matched_sell_at": matched_sell.get("created_at", ""),
                }
            elif buy_price > 0 and prices.get(market, 0.0) > 0:
                current_price = prices[market]
                order = {
                    **order,
                    "pnl_pct": (current_price / buy_price - 1.0) * 100.0,
                    "pnl_basis": "현재가",
                }
            else:
                order = {**order, "pnl_basis": "확인불가"}
        elif side == "ask" and not order.get("pnl_pct"):
            sell_time = str(order.get("created_at", ""))
            sell_price = _float(order.get("avg_price"))
            matched_buy = next(
                (
                    buy for buy in reversed(buys_by_market.get(market, []))
                    if str(buy.get("created_at", "")) < sell_time and _float(buy.get("avg_price")) > 0
                ),
                None,
            )
            buy_price = _float(matched_buy.get("avg_price")) if matched_buy else 0.0
            if buy_price > 0 and sell_price > 0:
                order = {
                    **order,
                    "pnl_pct": (sell_price / buy_price - 1.0) * 100.0,
                    "pnl_basis": "매수매칭",
                }

        enriched.append(order)
    return enriched


def _merge_trade_history(api_orders: list[dict[str, Any]], log_orders: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    merged_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    error_rows: list[dict[str, Any]] = []

    for order in sorted(api_orders + log_orders, key=lambda item: str(item.get("created_at", "")), reverse=True):
        if order.get("error"):
            error_rows.append(order)
            continue

        # API 주문과 봇 로그가 같은 체결을 중복 표시하지 않도록 초 단위 대신 분 단위로 묶는다.
        created_at = str(order.get("created_at", ""))
        key = (str(order.get("market", "")), str(order.get("side", "")), created_at[:16])
        existing = merged_by_key.get(key)
        if existing is None:
            merged_by_key[key] = order
            continue

        # 업비트 API의 체결금액/수량을 우선 보존하되, 봇 로그에만 있는 매도 사유와 수익률을 덧붙인다.
        if order.get("source") == "bot_log":
            existing.setdefault("source", "api")
            for field in ("memo", "sell_reason", "pnl_pct", "pnl_basis", "matched_sell_at"):
                if order.get(field) not in (None, ""):
                    existing[field] = order[field]
            if not existing.get("avg_price") and order.get("avg_price"):
                existing["avg_price"] = order["avg_price"]
            if not existing.get("funds") and order.get("funds"):
                existing["funds"] = order["funds"]
        elif existing.get("source") == "bot_log":
            for field, value in existing.items():
                if field in {"memo", "sell_reason", "pnl_pct", "pnl_basis", "matched_sell_at"} and value not in (None, ""):
                    order[field] = value
            merged_by_key[key] = order

    merged = sorted(
        list(merged_by_key.values()) + error_rows,
        key=lambda item: str(item.get("created_at", "")),
        reverse=True,
    )
    return merged[:limit]


def _recent_log_trades(limit: int = 30) -> list[dict[str, Any]]:
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-600:]
    except OSError:
        return []

    trades: list[dict[str, Any]] = []
    for line in reversed(lines):
        match = TRADE_RE.search(line)
        if not match:
            continue
        trades.append(match.groupdict())
        if len(trades) >= limit:
            break
    return trades


def _latest_dynamic_selection() -> dict[str, dict[str, Any]]:
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-1000:]
    except OSError:
        return {}

    selections: dict[str, dict[str, Any]] = {}
    for line in reversed(lines):
        match = TOP_SELECTION_RE.search(line)
        if not match:
            continue
        item = match.groupdict()
        slot = item["slot"]
        if slot in selections:
            continue
        selections[slot] = {
            "market": item["market"],
            "asset": item["market"].split("-", 1)[1],
            "selected_at": item["time"],
            "signed_change_rate": _float(item["rate"]) / 100.0,
            "selected_trade_price": _float(item["price"]),
            "source": "log",
        }
        if len(selections) == 2:
            break
    return selections


def _bot_status() -> dict[str, Any]:
    try:
        latest = LOG_FILE.stat().st_mtime
        age_seconds = max(0.0, datetime.now().timestamp() - latest)
        return {
            "log_exists": True,
            "last_log_at": datetime.fromtimestamp(latest).isoformat(),
            "age_seconds": age_seconds,
            "is_recent": age_seconds < 180,
        }
    except OSError:
        return {"log_exists": False, "last_log_at": None, "age_seconds": None, "is_recent": False}


def build_overview(limit: int = 30) -> dict[str, Any]:
    client = _get_upbit_client()
    balances_raw = client.get_balances()
    if not isinstance(balances_raw, list):
        raise RuntimeError(f"업비트 잔고 응답 형식이 올바르지 않습니다: {balances_raw}")

    balances = _normalize_balances(balances_raw)
    market_context = [_resolve_market(cfg) for cfg in MARKETS]
    tracked_markets = [market for market, _, _ in market_context if market.startswith("KRW-")]
    balance_markets = [
        f"KRW-{currency}"
        for currency in balances
        if currency != "KRW"
    ]
    prices = _current_prices(tracked_markets + balance_markets)

    krw_balance = balances.get("KRW", {}).get("balance", 0.0)
    total_coin_value = 0.0
    total_buy_value = 0.0
    for currency, item in balances.items():
        if currency == "KRW":
            continue
        qty = _float(item.get("balance")) + _float(item.get("locked"))
        if qty <= 0:
            continue
        market = f"KRW-{currency}"
        current_price = prices.get(market, 0.0)
        avg_buy_price = _float(item.get("avg_buy_price"))
        valuation_price = current_price if current_price > 0 else avg_buy_price
        total_coin_value += qty * valuation_price
        if avg_buy_price > 0:
            total_buy_value += qty * avg_buy_price

    positions: list[dict[str, Any]] = []

    for cfg, (market, asset, state) in zip(MARKETS, market_context):
        balance_info = balances.get(asset, {})
        balance = _float(balance_info.get("balance"))
        locked = _float(balance_info.get("locked"))
        avg_buy_price = _float(balance_info.get("avg_buy_price") or state.get("buy_price"))
        current_price = prices.get(market, 0.0)
        display_price = current_price if current_price > 0 else avg_buy_price
        value = (balance + locked) * display_price
        buy_value = (balance + locked) * avg_buy_price
        pnl_krw = value - buy_value if buy_value > 0 else 0.0
        pnl_pct = (pnl_krw / buy_value * 100.0) if buy_value > 0 else 0.0

        # 상태 파일과 실제 잔고를 같이 봐야, 봇 재시작/부분익절 이후의 화면 오차를 줄일 수 있다.
        has_position = value >= 5_000 or buy_value >= 5_000
        positions.append({
            **asdict(cfg),
            "market": market,
            "asset": asset,
            "state": state,
            "has_position": has_position,
            "balance": balance,
            "locked": locked,
            "avg_buy_price": avg_buy_price,
            "current_price": display_price,
            "value_krw": value,
            "buy_value_krw": buy_value,
            "pnl_krw": pnl_krw,
            "pnl_pct": pnl_pct,
            "take_profit_price": avg_buy_price * (1.0 + cfg.take_profit / 100.0) if avg_buy_price else 0.0,
            "stop_loss_price": avg_buy_price * (1.0 - cfg.stop_loss / 100.0) if avg_buy_price else 0.0,
            "chart": _ohlcv_points(market, 48) if market.startswith("KRW-") else [],
        })

    holdings: list[dict[str, Any]] = []
    tracked_assets = {asset for _, asset, _ in market_context}
    for currency, item in balances.items():
        if currency == "KRW":
            continue
        market = f"KRW-{currency}"
        price = prices.get(market, 0.0)
        qty = _float(item.get("balance")) + _float(item.get("locked"))
        avg_buy_price = _float(item.get("avg_buy_price"))
        display_price = price if price > 0 else avg_buy_price
        value = qty * display_price
        buy_value = qty * avg_buy_price
        if max(value, buy_value) < 1.0:
            continue
        holdings.append({
            "currency": currency,
            "market": market,
            "quantity": qty,
            "avg_buy_price": avg_buy_price,
            "current_price": display_price,
            "value_krw": value,
            "pnl_krw": value - buy_value if buy_value > 0 else 0.0,
            "pnl_pct": ((value - buy_value) / buy_value * 100.0) if buy_value > 0 else 0.0,
        })

    total_assets = krw_balance + total_coin_value
    total_pnl = total_coin_value - total_buy_value if total_buy_value > 0 else 0.0
    total_pnl_pct = total_pnl / total_buy_value * 100.0 if total_buy_value > 0 else 0.0

    api_orders = _order_history(client, tracked_markets, limit)
    log_orders = _log_trade_history(max(limit * 4, 200))
    log_markets = {
        str(order.get("market", ""))
        for order in log_orders
        if str(order.get("market", "")).startswith("KRW-")
    }
    extra_log_prices = _current_prices(sorted(log_markets - set(prices)))
    trade_prices = {**prices, **extra_log_prices}
    trade_history = _merge_trade_history(api_orders, log_orders, limit)
    trade_history = _enrich_trade_pnl(trade_history, trade_prices)

    summary = {
        "krw_balance": krw_balance,
        "coin_value": total_coin_value,
        "total_assets": total_assets,
        "total_buy_value": total_buy_value,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "active_positions": sum(1 for pos in positions if pos["has_position"]),
        "risk_exposure_pct": (total_coin_value / total_assets * 100.0) if total_assets > 0 else 0.0,
    }
    display_summary = _display_summary(summary)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": display_summary,
        "bot": _bot_status(),
        "positions": positions,
        "holdings": sorted(holdings, key=lambda item: item["value_krw"], reverse=True)[:30],
        "orders": trade_history,
        "log_trades": _recent_log_trades(limit),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "UpbitDashboard/1.0"

    def _allowed_cors_origin(self) -> str | None:
        origin = self.headers.get("Origin")
        allowed_raw = os.getenv("DASHBOARD_ALLOWED_ORIGINS", "*")
        allowed = {item.strip() for item in allowed_raw.split(",") if item.strip()}
        if "*" in allowed:
            return origin or "*"
        if origin and origin in allowed:
            return origin
        return None

    def _send_cors_headers(self) -> None:
        origin = self._allowed_cors_origin()
        if not origin:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/overview":
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["30"])[0])
            self._json_response(lambda: build_overview(limit=max(1, min(limit, 100))))
            return
        if parsed.path in {"/", "/index.html"}:
            self._file_response(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/app.js":
            self._file_response(STATIC_DIR / "app.js", "text/javascript; charset=utf-8")
            return
        if parsed.path == "/styles.css":
            self._file_response(STATIC_DIR / "styles.css", "text/css; charset=utf-8")
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[dashboard] {self.address_string()} - {fmt % args}")

    def _json_response(self, factory: Any) -> None:
        try:
            payload = factory()
            status = HTTPStatus.OK
        except Exception as exc:
            payload = {"error": str(exc), "type": type(exc).__name__}
            status = HTTPStatus.INTERNAL_SERVER_ERROR

        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file_response(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    httpd = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Upbit dashboard running at http://{host}:{port}")
    print("Read-only mode: balances, order history, logs and local state are displayed only.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
