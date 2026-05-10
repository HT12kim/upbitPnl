# ChangeLog

## 2026-05

- [매매전략 추가](/backtest_short_term_5m.py)
- 5분봉 백테스트 엔진에 `vwap_pullback` 진입 신호 추가
  - Rolling VWAP 기준가 계산
  - 최근 N봉 내 VWAP 터치 여부 확인
  - VWAP 상단 회복, 양봉, 거래량 필터를 결합한 눌림목 진입 후보 검증
- [최적화 스크립트 추가](/optimize_xrp_vwap_pullback.py)
  - XRP VWAP 눌림목 전략의 VWAP 윈도우, 터치 lookback, 거래량 배수, TP/SL, 보유시간 그리드 탐색
- [최적화 스크립트 추가](/optimize_xrp_trend_pullback.py)
  - XRP EMA 눌림목 전략의 EMA 조합, 기울기 lookback, TP/SL, trailing ATR, TP1 그리드 탐색
- [최적화 스크립트 추가](/optimize_xrp_more_trades.py)
  - XRP 변동성 돌파 전략의 거래 빈도 증가를 위한 세션, 요일, 변동성/거래량 임계값 완화 후보 탐색
- [최적화/검증 스크립트 추가](/optimize_per_market.py), [검증](/validate_per_market.py)
  - ETH/BTC 시장별 변동성 돌파 winner 후보 탐색 및 train/test 일관성 검증
- [알림 개선](/main_xrp_night.py)
  - XRP 단일 야간 봇의 자산현황 알림을 매분에서 매시간으로 변경
  - 포지션, 주요 가격 레벨, PnL, 자산현황, 진입 신호 근접도를 상세 표시

## 2025-03

- [매매전략 추가](/trading/bollinger_band_breakout.py)
- 볼린저 밴드(Bollinger Band)를 활용한 매매

## 2025-01 (3)

- [다른 전략 추가](/trading/trading_strategy2.py)
- EMA를 활용한 단기 트레이딩
- 추가한 전략에 대한 테스트 중

## 2025-01 (2)

- 전반적으로 매매전략 변경
- 매도 조건 추가  
  (매수 시점 이후에 한 번이라도 볼린저밴드의 상단을 돌파하는 경우가 있다면, 현재가가 볼린저밴드 중심 아래로 떨어지면 즉시 매도)
- MA 기준 변경 (50MA -> 20MA)
- 매수 타이밍을 매분 체크하도록 설정
- 매수 조건 추가 (거래량과 볼린저밴드 돌파에 대한 내용)
- 손절매 기준 변경 (0.69%% 손실 시 손절매)

## 2025-01

- 기준 변경 (15분봉 -> 10분봉)
- 매도 시, 매매 결과까지 알리도록 설정
- 매도 중에 'wait' 거래가 있으면 계속 대기했다가 다음 로직을 수행하도록 처리
- 골든 크로스, 데드 크로스에 대한 전략 추가

## 2024-12

- initial commit
