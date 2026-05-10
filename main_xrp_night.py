"""
KRW-XRP 5분봉 야간 변동성 돌파 전략 라이브 트레이딩

검증된 파라미터 (v3 walk-forward 통과):
  entry_kind = volatility_breakout
  vol_mult=1.5, atr_mult=1.5, atr_window=14
  volume_baseline=10,  breakout_lookback=10
  take_profit=2.0%,  stop_loss=1.5%,  max_hold=60봉(5시간)
  tp1_pct=1.2% / tp1_ratio=0.7  (70% 부분 익절)
  session=kr_night(00-09 KST),  weekday=월-금

상태 영속화: data_cache/xrp_night_state.json
  → 봇 재시작 후에도 buy_price / tp1_taken / buy_time 유지
"""

import sys
import os
import math
import time
import json
import logging
import logging.config
from datetime import datetime
from pathlib import Path

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler

# ── sys.path 설정 ──────────────────────────────────────────────────────────
_dir = os.path.dirname(os.path.abspath(__file__))
for _sub in ("account", "upbit_data", "trading", "utils"):
    sys.path.append(os.path.join(_dir, _sub))

from account.my_account import get_my_exchange_account
from upbit_data.candle import get_min_candle_data
from trading.trade import buy_market, sell_market
from utils.telegram_utils import send_telegram

# ── 상수 / 파라미터 ────────────────────────────────────────────────────────
MARKET      = "KRW-XRP"
ASSET       = "XRP"
STATE_FILE  = Path(_dir) / "data_cache" / "xrp_night_state.json"

VOL_MULT      = 1.5
ATR_MULT      = 1.5
ATR_WINDOW    = 14
VOL_BASELINE  = 10
LB            = 10
TAKE_PROFIT   = 2.0
STOP_LOSS     = 1.5
MAX_HOLD_BARS = 60
TP1_PCT       = 1.2
TP1_RATIO     = 0.7
MIN_ORDER_KRW = 5_000

_last_status_hour: int = -1   # 마지막으로 자산현황을 보낸 시각(hour)

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


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return _default_state()


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def clear_state() -> None:
    save_state(_default_state())


# ── 인디케이터 계산 ────────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> dict:
    """1000봉 DataFrame(오름차순)에서 마지막 완성 캔들(iloc[-2]) 기준 값 반환."""
    close  = df["close"].astype(float)
    high   = df["high"].astype(float)
    low    = df["low"].astype(float)
    volume = df["volume"].astype(float)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / ATR_WINDOW, adjust=False, min_periods=ATR_WINDOW).mean()

    vol_ma    = volume.rolling(VOL_BASELINE, min_periods=1).mean()
    vol_ratio = (volume / vol_ma).replace([float("inf"), float("-inf")], 0).fillna(0)

    rolling_high_prev  = high.shift(1).rolling(LB, min_periods=1).max()
    breakout_threshold = rolling_high_prev + atr * ATR_MULT

    i = -2
    return {
        "close":     float(close.iloc[i]),
        "vol_ratio": float(vol_ratio.iloc[i]),
        "bt":        float(breakout_threshold.iloc[i]),
        "atr":       float(atr.iloc[i]),
    }


# ── 계좌 조회 ──────────────────────────────────────────────────────────────

def get_account_info() -> dict:
    acct = get_my_exchange_account()

    has_xrp       = ASSET in acct["currency"].values
    xrp_balance   = "0"
    xrp_avg_price = 0.0

    if has_xrp:
        row           = acct[acct["currency"] == ASSET]
        xrp_balance   = str(row["balance"].values[0])
        xrp_avg_price = float(row["avg_buy_price"].values[0])

    krw_amount = 0.0
    if "KRW" in acct["currency"].values:
        krw_amount = float(acct[acct["currency"] == "KRW"]["balance"].astype(float).values[0])

    return {
        "has_xrp":       has_xrp,
        "xrp_balance":   xrp_balance,
        "xrp_avg_price": xrp_avg_price,
        "krw_balance":   krw_amount,
        "krw_available": math.floor(krw_amount * 0.999),
    }


# ── 필터 / 유틸 ───────────────────────────────────────────────────────────

def passes_filter(now: datetime) -> bool:
    return 0 <= now.hour < 9 and now.weekday() < 5


def hold_bars_elapsed(buy_time_iso: str, now: datetime) -> int:
    if not buy_time_iso:
        return 0
    buy_dt = datetime.fromisoformat(buy_time_iso)
    return int((now - buy_dt).total_seconds() // 300)


def truncate_qty(qty: float, decimals: int = 8) -> str:
    factor = 10 ** decimals
    return str(math.floor(qty * factor) / factor)


# ── 주문 결과 파싱 ─────────────────────────────────────────────────────────

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


# ── 텔레그램 메시지 빌더 ───────────────────────────────────────────────────

def _status_msg(now: datetime, account: dict, state: dict,
                cur_price: float, indic: dict) -> str:
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"📊 [KRW-XRP 시간현황] {ts}"]

    if account["has_xrp"]:
        buy_price = state.get("buy_price") or account["xrp_avg_price"]
        tp1_taken = bool(state.get("tp1_taken", False))
        hold_bars = hold_bars_elapsed(state.get("buy_time"), now)
        xrp_qty   = float(account["xrp_balance"])
        pnl_pct   = (cur_price / buy_price - 1) * 100 if buy_price > 0 else 0.0
        pnl_krw   = (cur_price - buy_price) * xrp_qty if buy_price > 0 else 0.0
        xrp_value = xrp_qty * cur_price
        total_est = xrp_value + account["krw_balance"]
        hold_h    = hold_bars * 5 // 60
        hold_m    = hold_bars * 5 % 60
        sl_price  = buy_price * (1.0 - STOP_LOSS / 100.0)
        tp1_price = buy_price * (1.0 + TP1_PCT / 100.0)
        tp_price  = buy_price * (1.0 + TAKE_PROFIT / 100.0)

        lines.append("▶ 포지션: LONG")
        lines.append(f"현재가:  {cur_price:,.2f}원")
        lines.append(f"매수가:  {buy_price:,.2f}원")
        lines.append(f"PnL:    {pnl_pct:+.2f}%  ({pnl_krw:+,.0f}원)")
        lines.append(f"보유봉:  {hold_bars}/{MAX_HOLD_BARS}봉  ({hold_h}h {hold_m}m)")
        lines.append(f"XRP수량: {xrp_qty:.4f} XRP")
        lines.append("")
        lines.append("── 주요 가격 레벨 ──")
        lines.append(f"손절:   {sl_price:,.2f}원  (-{STOP_LOSS}%)")
        if not tp1_taken:
            lines.append(f"TP1:    {tp1_price:,.2f}원  (+{TP1_PCT}%, {TP1_RATIO*100:.0f}% 부분익절)")
        else:
            lines.append(f"TP1:    완료 ✓")
        lines.append(f"익절:   {tp_price:,.2f}원  (+{TAKE_PROFIT}%)")
        lines.append("")
        lines.append("── 자산현황 ──")
        lines.append(f"XRP평가: {xrp_value:,.0f}원")
        lines.append(f"KRW잔고: {account['krw_balance']:,.0f}원")
        lines.append(f"총평가:  {total_est:,.0f}원")
    else:
        in_session = passes_filter(now)
        session_str = "✅ 진입가능 (kr_night+평일)" if in_session else "⏸ 세션 외"
        bt = indic["bt"]
        vol_ratio = indic["vol_ratio"]
        bt_gap_pct = (bt / cur_price - 1) * 100 if cur_price > 0 else 0.0

        lines.append("▶ 포지션: 없음")
        lines.append(f"세션:    {session_str}")
        lines.append(f"현재가:  {cur_price:,.2f}원")
        lines.append("")
        lines.append("── 진입 신호 근접도 ──")
        lines.append(f"돌파임계: {bt:,.2f}원  (현재가 대비 {bt_gap_pct:+.2f}%)")
        lines.append(f"거래량비: {vol_ratio:.2f}x  (기준 {VOL_MULT}x)")
        vol_bar = "🟢" if vol_ratio >= VOL_MULT else "🔴"
        price_bar = "🟢" if cur_price > bt else "🔴"
        lines.append(f"가격돌파: {price_bar}  거래량: {vol_bar}")
        lines.append("")
        lines.append("── 자산현황 ──")
        lines.append(f"KRW잔고: {account['krw_balance']:,.0f}원")

    return "\n".join(lines)


def _startup_msg(now: datetime) -> str:
    return (
        f"[XRP Night v3] 봇 시작\n"
        f"{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"\n--- 투자전략 요약 ---\n"
        f"마켓    : {MARKET}\n"
        f"세션    : kr_night 00-09 KST (평일)\n"
        f"\n"
        f"vol_mult : {VOL_MULT}\n"
        f"atr_mult : {ATR_MULT} x ATR{ATR_WINDOW}\n"
        f"lb       : {LB}봉\n"
        f"vol_base : {VOL_BASELINE}봉\n"
        f"\n"
        f"손절     : {STOP_LOSS}%\n"
        f"익절     : {TAKE_PROFIT}% (전량)\n"
        f"TP1      : {TP1_PCT}% ({TP1_RATIO*100:.0f}% 부분익절)\n"
        f"보유한도 : {MAX_HOLD_BARS}봉 ({MAX_HOLD_BARS * 5 // 60}시간)"
    )


# ── 메인 트레이딩 루틴 ─────────────────────────────────────────────────────

def auto_trading() -> None:
    try:
        now     = datetime.now()
        account = get_account_info()
        state   = load_state()
        candles = get_min_candle_data(MARKET, 5)
        indic   = compute_indicators(candles)
        cur_price = indic["close"]

        # ── 매시간 자산현황 알림 ────────────────────────────────────────────
        global _last_status_hour
        if now.hour != _last_status_hour:
            send_telegram(_status_msg(now, account, state, cur_price, indic))
            _last_status_hour = now.hour

        # ── 5분봉 경계에서만 트레이딩 로직 실행 ────────────────────────────
        if now.minute % 5 != 0:
            return

        logger.info(
            f"===== {MARKET} auto_trading @ {now.strftime('%Y-%m-%d %H:%M:%S')} ====="
        )
        logger.info(
            f"pos={'LONG' if account['has_xrp'] else 'NONE'}  "
            f"close={cur_price:.5f}  vol_ratio={indic['vol_ratio']:.2f}  bt={indic['bt']:.5f}"
        )

        # ================================================================
        # 포지션 있음 → 청산 조건 체크
        # ================================================================
        if account["has_xrp"]:
            buy_price = state.get("buy_price") or account["xrp_avg_price"]
            tp1_taken = bool(state.get("tp1_taken", False))
            hold_bars = hold_bars_elapsed(state.get("buy_time"), now)

            logger.info(
                f"buy_price={buy_price:.5f}  hold_bars={hold_bars}/{MAX_HOLD_BARS}  "
                f"tp1_taken={tp1_taken}"
            )

            # 1. 손절
            if cur_price <= buy_price * (1.0 - STOP_LOSS / 100.0):
                logger.info(f"[SELL] 손절  price={cur_price:.5f}  buy={buy_price:.5f}")
                res = sell_market(MARKET, account["xrp_balance"])
                ok, reason = _check_order(res)
                if ok:
                    pnl = (cur_price / buy_price - 1) * 100
                    logger.info(f"손절 체결  PnL≈{pnl:.2f}%")
                    send_telegram(
                        f"[KRW-XRP] 손절 체결\n"
                        f"매도가: {cur_price:,.0f}원  매수가: {buy_price:,.0f}원\n"
                        f"PnL: {pnl:+.2f}%"
                    )
                    clear_state()
                else:
                    logger.error(f"손절 주문 실패: {reason}")
                    send_telegram(f"[KRW-XRP] 손절 실패\n사유: {reason}")
                return

            # 2. 전량 익절
            if cur_price >= buy_price * (1.0 + TAKE_PROFIT / 100.0):
                logger.info(f"[SELL] 전량 익절  price={cur_price:.5f}  buy={buy_price:.5f}")
                res = sell_market(MARKET, account["xrp_balance"])
                ok, reason = _check_order(res)
                if ok:
                    pnl = (cur_price / buy_price - 1) * 100
                    logger.info(f"익절 체결  PnL≈{pnl:.2f}%")
                    send_telegram(
                        f"[KRW-XRP] 익절 체결 (전량)\n"
                        f"매도가: {cur_price:,.0f}원  매수가: {buy_price:,.0f}원\n"
                        f"PnL: {pnl:+.2f}%"
                    )
                    clear_state()
                else:
                    logger.error(f"익절 주문 실패: {reason}")
                    send_telegram(f"[KRW-XRP] 익절 실패\n사유: {reason}")
                return

            # 3. TP1 부분 익절
            if not tp1_taken and cur_price >= buy_price * (1.0 + TP1_PCT / 100.0):
                xrp_qty     = float(account["xrp_balance"])
                tp1_qty     = xrp_qty * TP1_RATIO
                tp1_krw_est = tp1_qty * cur_price
                if tp1_krw_est >= MIN_ORDER_KRW:
                    sell_qty_str = truncate_qty(tp1_qty)
                    logger.info(
                        f"[SELL] TP1 부분익절 {TP1_RATIO*100:.0f}%  qty={sell_qty_str}"
                    )
                    res = sell_market(MARKET, sell_qty_str)
                    ok, reason = _check_order(res)
                    if ok:
                        pnl       = (cur_price / buy_price - 1) * 100
                        remaining = xrp_qty - tp1_qty
                        logger.info(
                            f"TP1 체결  PnL≈{pnl:.2f}%  잔여≈{remaining:.4f} XRP"
                        )
                        send_telegram(
                            f"[KRW-XRP] TP1 부분익절 {TP1_RATIO*100:.0f}% 체결\n"
                            f"매도가: {cur_price:,.0f}원  매수가: {buy_price:,.0f}원\n"
                            f"PnL: {pnl:+.2f}%\n"
                            f"잔여: {remaining:.4f} XRP"
                        )
                        state["tp1_taken"] = True
                        save_state(state)
                    else:
                        logger.error(f"TP1 주문 실패: {reason}")
                        send_telegram(f"[KRW-XRP] TP1 실패\n사유: {reason}")
                else:
                    logger.warning(
                        f"TP1 잔여 금액({tp1_krw_est:.0f}원) < 최소주문({MIN_ORDER_KRW}원), 건너뜀"
                    )
                return

            # 4. 최대 보유봉 초과 → 시간 청산
            if hold_bars >= MAX_HOLD_BARS:
                logger.info(f"[SELL] 시간 청산  hold_bars={hold_bars}/{MAX_HOLD_BARS}")
                res = sell_market(MARKET, account["xrp_balance"])
                ok, reason = _check_order(res)
                if ok:
                    pnl = (cur_price / buy_price - 1) * 100
                    logger.info(f"시간 청산 체결  PnL≈{pnl:.2f}%")
                    send_telegram(
                        f"[KRW-XRP] 시간청산 체결\n"
                        f"매도가: {cur_price:,.0f}원  매수가: {buy_price:,.0f}원\n"
                        f"PnL: {pnl:+.2f}%  보유봉: {hold_bars}/{MAX_HOLD_BARS}"
                    )
                    clear_state()
                else:
                    logger.error(f"시간청산 주문 실패: {reason}")
                    send_telegram(f"[KRW-XRP] 시간청산 실패\n사유: {reason}")
                return

            logger.info("보유 중 — 청산 조건 없음")

        # ================================================================
        # 포지션 없음 → 진입 조건 체크
        # ================================================================
        else:
            if state.get("buy_price", 0) > 0:
                logger.info("포지션 없음 + 상태파일 잔존 → 초기화")
                clear_state()
                state = _default_state()

            if not passes_filter(now):
                logger.debug("세션/요일 필터 미통과 → 패스")
                return

            entry = cur_price > indic["bt"] and indic["vol_ratio"] >= VOL_MULT
            logger.info(
                f"진입신호: {entry}  "
                f"(close={cur_price:.5f} > bt={indic['bt']:.5f}, "
                f"vol_ratio={indic['vol_ratio']:.2f} >= {VOL_MULT})"
            )

            if not entry:
                return

            if account["krw_available"] < MIN_ORDER_KRW:
                msg = f"투자가능금액({account['krw_available']}원) < 최소주문"
                logger.warning(msg)
                send_telegram(f"[KRW-XRP] 매수 불가\n사유: {msg}")
                return

            res = buy_market(MARKET, account["krw_available"])
            ok, reason = _check_order(res)
            if ok:
                time.sleep(2)
                try:
                    after_acct = get_my_exchange_account()
                    if ASSET in after_acct["currency"].values:
                        actual_buy_price = float(
                            after_acct[after_acct["currency"] == ASSET]["avg_buy_price"].values[0]
                        )
                    else:
                        actual_buy_price = cur_price
                except Exception:
                    actual_buy_price = cur_price

                logger.info(
                    f"[BUY] {account['krw_available']}원 매수 완료  "
                    f"avg_buy_price={actual_buy_price:.5f}"
                )
                save_state({
                    "buy_price": actual_buy_price,
                    "tp1_taken": False,
                    "buy_time":  now.isoformat(),
                })
                send_telegram(
                    f"[KRW-XRP] 매수 체결\n"
                    f"매수가: {actual_buy_price:,.0f}원\n"
                    f"투자금: {account['krw_available']:,}원"
                )
            else:
                logger.error(f"매수 주문 실패: {reason}")
                send_telegram(f"[KRW-XRP] 매수 실패\n사유: {reason}")

    except ValueError as ve:
        logger.error(f"ValueError: {ve}")
    except Exception as e:
        logger.error(f"예상치 못한 오류: {e}", exc_info=True)


# ── 진입점 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("++++++++++ XRP night strategy (v3) starts. ++++++++++")
    logger.info(
        f"파라미터: vol={VOL_MULT}, atr={ATR_MULT}×ATR{ATR_WINDOW}, lb={LB}, vol_base={VOL_BASELINE}  |  "
        f"SL={STOP_LOSS}%  TP={TAKE_PROFIT}%  TP1={TP1_PCT}%×{TP1_RATIO*100:.0f}%  MAX_HOLD={MAX_HOLD_BARS}봉"
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
        send_telegram(
            f"[XRP Night v3] 봇 종료\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
