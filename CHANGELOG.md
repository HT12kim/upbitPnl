# ChangeLog

## 2026-05

- [동적 TOP 상승률 슬롯 확장](/main_multi_market.py)
  - 전일대비 상승률 상위 KRW 종목을 기존 1개에서 2개 슬롯(TOP1/TOP2)으로 확대
  - 슬롯별 상태 파일을 분리해 `top_gainer_state.json`, `top_gainer_2_state.json`에 저장
  - 고정 3개 시장과 동적 슬롯 간 중복 선정 방지, 보유 중인 슬롯은 강제 교체 없이 유지
  - 동적 슬롯 전략을 거래기회 확대형 변동성 돌파로 조정
    - `vol=2.5`, `atr=1.0`, `vbase=10`, `lb=10`, `TP=2.0%`, `SL=1.5%`, `hold=60`, `all/all`
- [전략 개선](/main_multi_market.py), [검증 스크립트](/compare_strategy_upgrade.py)
  - ETH 변동성 돌파 전략을 거래기회 확대 후보(ETH F1)로 변경
    - 1년 백테스트 기준 수익률 `+17.39% → +23.39%`, 거래 `41회 → 175회`
  - BTC 라이브 설정을 검증된 `kr_day/all` winner로 정정
    - 코드상 `all/all` 설정의 1년 백테스트 `-13.32%`를 `+9.81%` 후보로 개선
  - XRP는 1년 기준 수익률 유지 조건을 만족하는 거래확대 후보가 확인되지 않아 기존 v3 유지
- [동적 TOP 상승률 슬롯 추가](/main_multi_market.py)
  - 기존 `KRW-XRP`, `KRW-ETH`, `KRW-BTC` 3개 고정 시장 유지
  - `ticker/all?quote_currencies=KRW` 기준 전일대비 상승률 1위 KRW 종목을 1시간마다 선정
  - 동일 계좌 포지션 중복 관리를 피하기 위해 동적 후보에서는 고정 3개 시장 제외
  - 신규 상장/거래 이력 부족 종목으로 인한 빈 캔들 오류를 막기 위해 5분봉 200개 미만 후보 제외
  - 최소 매도금액 미만 소량 잔고를 포지션으로 오인해 반복 매도 실패 알림이 발생하지 않도록 포지션 판단 기준 보강
  - 동적 슬롯은 BTC W1 기본 파라미터의 5분봉 변동성 돌파 전략을 동일하게 적용
  - 보유 중에는 TOP 종목이 바뀌어도 강제 교체하지 않고 TP/SL/시간청산까지 기존 포지션 유지
  - 매수/매도 및 시간별 텔레그램 알림에 동적 슬롯 선정 사유와 선정 상태 표시
- [매수 한도 정책 변경](/main_multi_market.py)
  - 시장별 고정 매수한도 `333,000원` 제거
  - 매수 신호 발생 시점의 현재 가용 KRW 잔고 전체를 주문금액으로 사용
  - 시작/상태/매수 체결 텔레그램 알림에 현재 가용 KRW 기준 한도 표시
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
