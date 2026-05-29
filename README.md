# UPbit Multi-Market Auto Trading Bot

업비트 원화 마켓에서 `KRW-XRP`, `KRW-ETH`, `KRW-BTC`와 동적 `TOP_GAINER`를 동시에 감시하고, 마지막 완성 5분봉 기준으로 시장가 매수/매도를 수행하는 Python 자동매매 봇입니다.

> 실거래 주문을 실행하는 코드입니다. 반드시 소액 또는 모의 환경에 준하는 검증 후 사용하세요.

## 핵심 기능

- `main_multi_market.py` 중심의 멀티마켓 라이브 트레이딩
- 매분 실행하되 마지막 완성 5분봉만 기준으로 판단
- 5분봉 1,000개 데이터를 기반으로 ATR 변동성 돌파 및 거래량 필터 계산
- 시장별 독립 파라미터, 진입 세션, 보유 한도, TP/SL 관리
- 시장가 매수, 시장가 전량 매도, XRP TP1 부분익절 지원
- 상태 파일 기반 포지션 복구
- 텔레그램 시작/종료/체결/실패/오류/시간별 자산현황 알림
- 시간별 알림에 직전 1시간 투자 판단 결과 요약 포함
- 백테스트, 최적화, 검증 스크립트 포함

## 주의 사항

- 이 프로젝트는 투자 수익을 보장하지 않습니다.
- 업비트 API 키는 출금 권한 없이 발급하는 것을 권장합니다.
- `.env`, 로그, 상태 파일, 캐시 파일은 Git에 올리지 마세요.
- 현재 시장가 주문 성공 여부는 업비트 주문 응답의 `uuid` 존재 여부로 판단합니다. 더 엄밀한 운영이 필요하면 주문 UUID로 체결 상태를 재조회하는 로직을 추가하세요.
- `utils/telegram_utils.py`에 기본 토큰/채팅 ID 값이 들어가 있다면 실제 운영 전 환경 변수 기반으로만 쓰도록 정리하는 것이 안전합니다.

## 실행 대상 파일

주된 라이브봇 파일은 다음입니다.

```shell
python main_multi_market.py
```

보조/레거시 실행 파일도 존재하지만, 현재 멀티마켓 실거래 기준 문서는 `main_multi_market.py`를 기준으로 작성되어 있습니다.

## 전략 개요

봇은 매분 5초에 실행되지만, 실제 투자 판단은 마지막 완성 5분봉을 기준으로 수행합니다. 같은 5분봉 신호는 1분 단위 재실행 중에도 중복 처리하지 않습니다.

판단에는 업비트 5분봉 API를 5회 호출하여 최대 1,000개 캔들을 사용합니다. 캔들은 시간 오름차순으로 정렬한 뒤 가장 최근 완성 캔들인 `iloc[-2]`를 기준으로 시그널을 계산합니다.

### 진입 조건

무포지션 상태에서 다음 조건을 모두 만족하면 시장가 매수를 시도합니다.

```text
현재 종가 > 돌파 임계값
거래량 비율 >= 시장별 vol_mult
시장별 세션/요일 필터 통과
주문 가능 KRW >= 최소주문금액
```

돌파 임계값은 다음 방식으로 계산합니다.

```text
rolling_high_prev = 이전 고가 기준 lb 기간 rolling max
ATR = True Range의 EMA
breakout_threshold = rolling_high_prev + ATR * atr_mult
```

거래량 비율은 현재 거래량을 `vol_baseline` 기간 이동평균 거래량으로 나눈 값입니다.

### 청산 조건

포지션 보유 상태에서는 아래 순서로 청산 조건을 검사합니다.

1. 손절: 현재가가 매수가 대비 `stop_loss` 이하
2. 전량 익절: 현재가가 매수가 대비 `take_profit` 이상
3. KST 09:00 일괄 청산: 전일 보유분은 오전 9시부터 전량 청산
4. TP1 부분익절: TP1 활성 시장에서 1차 익절 조건 충족

위 조건 중 하나가 실행되면 해당 시장 처리는 즉시 종료하고 다음 시장으로 넘어갑니다.

## 시장별 기본 파라미터

| Market | Label | 진입 세션 | 요일 | 진입 엔진 | TP | SL | Max hold | TP1 | 비고 |
|---|---|---|---|---|---:|---:|---:|---|---|
| `KRW-XRP` | XRP v3 | 00-09시 KST | 전체 | `volatility_breakout` | 2.0% | 1.5% | 60봉 | 1.2% 도달 시 70% | 최근 90일 1분봉 세션 검증 반영 |
| `KRW-ETH` | ETH freq-balanced | 09-18시 KST | 평일 | `volatility_breakout` | 2.0% | 1.5% | 60봉 | 비활성 | 최근 1년 검증에서 거래빈도 균형 후보 |
| `KRW-BTC` | BTC W1 | 09-18시 KST | 전체 | `volatility_breakout` | 3.0% | 3.0% | 48봉 | 비활성 | 5분봉 기준 유지 |
| `TOP_GAINER` | TOP2 dynamic | all | all | `volatility_breakout` | 4.0% | 2.0% | 36봉 | 1.2% 도달 시 70% | KRW 마켓 전일대비 상승률 TOP 2, 필터 적용 |

`max_hold_bars`는 전략 설정값으로 남아 있고, 실질 강제 청산은 KST 09:00 일괄 청산입니다.

```text
60봉 = 300분 = 5시간
48봉 = 240분 = 4시간
24봉 = 120분 = 2시간
```

## 텔레그램 알림

`utils/telegram_utils.py`의 `send_telegram()`을 통해 텔레그램 메시지를 발송합니다.

알림이 발송되는 시점은 다음과 같습니다.

- 봇 시작
- 봇 종료
- 매수 체결
- 매수 실패 또는 KRW 부족으로 매수 보류
- 손절 체결 또는 실패
- 전량 익절 체결 또는 실패
- TP1 부분익절 체결 또는 실패
- 시간청산 체결 또는 실패
- 시장별 처리 중 예외 발생
- 매시간 통합 자산 현황

시간별 통합 자산 현황에는 다음이 포함됩니다.

- 시장별 현재 포지션 상태
- 현재가, 매수가, 보유시간, 평가손익
- SL/TP/TP1 기준가
- KRW 잔고, 코인 평가액, 총 평가액
- 직전 1시간 투자 판단 요약

직전 1시간 투자 판단 요약에는 시장별로 다음이 표시됩니다.

- 판단 횟수
- 세션 내 판단 횟수
- 거래/시도 횟수
- 보유 유지 횟수
- 무신호 횟수
- 세션 외 횟수
- 최근 거래/시도 상세

## 상태 파일

포지션 상태는 시장별 JSON 파일에 저장됩니다.

```text
data_cache/xrp_night_state.json
data_cache/eth_state.json
data_cache/btc_state.json
```

저장되는 주요 값은 다음과 같습니다.

```json
{
  "buy_price": 0.0,
  "tp1_taken": false,
  "buy_time": null
}
```

봇 재시작 후에도 기존 포지션의 매수가, TP1 처리 여부, 매수 시각을 이어서 사용할 수 있습니다. 실제 업비트 잔고와 상태 파일이 어긋나면 무포지션 확인 시 상태 파일을 초기화합니다.

## 설치

Python 3.9 이상 환경을 권장합니다.

```shell
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 환경 변수

프로젝트 루트에 `.env` 파일을 만들고 다음 값을 설정합니다.

```shell
ACCESS_KEY=your_upbit_access_key
SECRET_KEY=your_upbit_secret_key
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

업비트 API 키는 업비트 웹에서 발급합니다.

```text
마이페이지 > Open API 관리 > 키 발급
```

권장 권한은 다음과 같습니다.

- 자산 조회
- 주문 조회
- 주문하기
- 출금 권한 제외
- 가능하면 고정 IP 제한 사용

## 실행 전 체크리스트

실행 전 아래 항목을 확인하세요.

- `logs/` 디렉터리가 존재하는지 확인
- `logging.conf`의 로그 파일 경로가 현재 로컬 경로와 맞는지 확인
- `.env`에 업비트/텔레그램 키가 설정되어 있는지 확인
- 매수 신호 발생 시 현재 가용 KRW 잔고 전체가 주문금액으로 사용되는지 확인
- 기존 업비트 보유 잔고와 `data_cache/*.json` 상태가 충돌하지 않는지 확인
- 네트워크 연결과 업비트 API 접근이 정상인지 확인

## 실행

```shell
source .venv/bin/activate
python main_multi_market.py
```

백그라운드 운영 예시는 다음과 같습니다.

```shell
nohup python main_multi_market.py > logs/bot.out 2>&1 &
```

종료는 일반적으로 `Ctrl+C` 또는 프로세스 종료를 사용합니다. 정상 종료 경로에서는 텔레그램 종료 알림이 발송됩니다.

## 로컬 투자 현황 대시보드

`pyupbit` 기반 읽기 전용 대시보드를 함께 제공합니다. 잔고, 자동매매 슬롯, 현재가, 수익률, 주문 이력, 로컬 봇 로그 상태를 한 화면에서 확인할 수 있습니다.

```shell
python3 -m dashboard.server
```

기본 접속 주소는 다음입니다.

```text
http://127.0.0.1:8080
```

다른 포트를 쓰려면 환경 변수를 지정합니다.

```shell
DASHBOARD_PORT=8081 python3 -m dashboard.server
```

주의사항:

- 대시보드는 주문 API를 호출하지 않고 조회 API만 사용합니다.
- `.env`의 `ACCESS_KEY`, `SECRET_KEY`가 필요합니다.
- 배포 시에는 API 키가 서버 환경 변수에만 존재하도록 구성하고, 정적 파일에 키를 노출하지 마세요.
- 현재 프론트엔드는 빠른 로컬 검증을 위해 Tailwind CDN을 사용합니다. 운영 배포에서는 Tailwind CLI/PostCSS 빌드 또는 Next.js 프론트엔드로 분리하는 구성을 권장합니다.

## Netlify + 로컬 API 연동

Netlify에는 정적 프론트만 배포하고, 실제 잔고/주문/로그 API는 내 PC에서 실행하는 구조입니다.

```text
Netlify 정적 프론트
  -> /api/* 요청
  -> Netlify _redirects
  -> Cloudflare Tunnel HTTPS 주소
  -> 내 PC의 dashboard.server
```

### Netlify Build settings

Netlify 사이트 설정은 다음처럼 둡니다.

```text
Base directory: 비워둠
Build command: 비워둠
Publish directory: dashboard/static
```

`dashboard/static/_redirects`가 Netlify의 `/api/*` 요청을 현재 Cloudflare Tunnel 주소로 프록시합니다.

```text
/api/*  https://cohen-profession-spiritual-chronicles.trycloudflare.com/api/:splat  200
```

Cloudflare Quick Tunnel 주소는 임시 주소입니다. `cloudflared tunnel --url ...`을 다시 실행하면 주소가 바뀔 수 있습니다. 주소가 바뀌면 `_redirects`의 도메인을 새 주소로 바꾼 뒤 커밋/푸시하고 Netlify를 재배포해야 합니다.

### 터미널 실행 순서

터미널 1: 로컬 API 서버 실행

```shell
cd /Users/ht_mac_mini/Documents/dev/git_btc_try2/UPbitAutoTrading-main
DASHBOARD_ALLOWED_ORIGINS="*" python3 -m dashboard.server
```

특정 Netlify 도메인만 허용하려면 `*` 대신 실제 Netlify 주소를 넣습니다.

```shell
DASHBOARD_ALLOWED_ORIGINS="https://YOUR-SITE.netlify.app" python3 -m dashboard.server
```

터미널 2: Cloudflare Tunnel 실행

```shell
cloudflared tunnel --url http://127.0.0.1:8080
```

`cloudflared`가 없다면 Homebrew로 설치합니다.

```shell
brew install cloudflared
cloudflared --version
```

터미널 3: 터널 API 확인

```shell
curl "https://cohen-profession-spiritual-chronicles.trycloudflare.com/api/overview?limit=1"
```

정상이라면 JSON이 출력됩니다. `<!DOCTYPE html>`이 나오면 API가 아니라 HTML을 받은 것이므로 터널 주소나 `_redirects` 설정을 다시 확인해야 합니다.

### Netlify 접속 방법

`_redirects`가 최신 터널 주소를 가리키고 있으면 기본 Netlify 주소만 접속하면 됩니다.

```text
https://YOUR-SITE.netlify.app
```

프론트에서 임시로 다른 API 주소를 쓰고 싶으면 `api` 쿼리 파라미터를 사용할 수 있습니다.

```text
https://YOUR-SITE.netlify.app/?api=https://새로운-터널주소.trycloudflare.com
```

이 값은 브라우저 `localStorage`에 저장됩니다. 잘못된 주소가 저장됐다면 브라우저 개발자 콘솔에서 아래 명령으로 초기화합니다.

```javascript
localStorage.removeItem("UPBIT_API_BASE_URL")
```

### 자주 나는 오류

`ERR_NAME_NOT_RESOLVED`

```text
Cloudflare Tunnel 주소가 만료됐거나 cloudflared 프로세스가 꺼진 상태입니다.
cloudflared tunnel --url http://127.0.0.1:8080 을 다시 실행하고 새 주소로 _redirects를 갱신하세요.
```

`Unexpected token '<', "<!DOCTYPE "... is not valid JSON`

```text
API가 JSON이 아니라 HTML을 반환한 상태입니다.
대부분 Netlify index.html, Cloudflare 에러 페이지, 또는 잘못된 터널 주소를 받은 경우입니다.
curl "터널주소/api/overview?limit=1" 로 JSON 출력 여부를 먼저 확인하세요.
```

`curl: (6) Could not resolve host: https`

```text
URL에 https://를 두 번 입력한 경우가 많습니다.
curl "https://도메인.trycloudflare.com/api/overview?limit=1" 처럼 한 번만 입력하세요.
```

## 스케줄

`APScheduler`의 `BackgroundScheduler`를 사용합니다.

```python
scheduler.add_job(auto_trading, "cron", second=5)
```

동작 구조는 다음과 같습니다.

- 매분 5초: `auto_trading()` 실행
- 매 실행: 계좌 조회, 시장별 캔들/지표 스냅샷 수집
- 매 실행: 마지막 완성 5분봉 기준으로 진입/청산 여부 판단
- 매시간 hour 변경 시: 통합 자산 현황 및 직전 1시간 판단 요약 전송

## 프로젝트 구조

```text
.
├── account/
│   └── my_account.py              # 업비트 계좌 조회
├── trading/
│   ├── trade.py                   # 업비트 시장가 매수/매도
│   ├── trading_strategy.py         # 레거시 전략
│   ├── trading_strategy2.py        # 레거시 전략
│   └── bollinger_band_breakout.py  # 볼린저밴드 전략
├── upbit_data/
│   └── candle.py                  # 업비트 분봉 데이터 조회
├── utils/
│   ├── telegram_utils.py           # 텔레그램 알림
│   └── email_utils.py              # 이메일 알림 유틸
├── main_multi_market.py            # 현재 주력 멀티마켓 라이브봇
├── main.py                         # 레거시 단일 봇
├── main_xrp_night.py               # XRP 야간 전략 봇
├── main_bb_breakout.py             # 볼린저밴드 돌파 봇
├── backtest_*.py                   # 백테스트 스크립트
├── optimize_*.py                   # 파라미터 최적화 스크립트
├── validate_*.py                   # 검증 스크립트
├── logging.conf                    # 로깅 설정
├── requirements.txt
└── README.md
```

## 주요 모듈 설명

### `account/my_account.py`

업비트 `/v1/accounts` API를 호출해 KRW 잔고와 보유 코인 정보를 조회합니다. `ACCESS_KEY`, `SECRET_KEY`는 `.env`에서 읽습니다.

### `upbit_data/candle.py`

업비트 분봉 API를 호출합니다. 1회 최대 200개 제한이 있으므로 5회 호출해 최대 1,000개 캔들을 구성합니다.

### `trading/trade.py`

업비트 `/v1/orders` API로 시장가 주문을 실행합니다.

- 매수: `ord_type=price`
- 매도: `ord_type=market`

### `utils/telegram_utils.py`

텔레그램 Bot API의 `sendMessage`를 사용해 알림을 보냅니다. 전송 실패 시 예외를 밖으로 던지지 않고 `False`를 반환합니다.

## 로그

로그 설정 파일은 `logging.conf`입니다.

기본 설정은 다음과 같습니다.

- 콘솔 로그 레벨: `DEBUG`
- 파일 로그 레벨: `INFO`
- 로그 회전: 매일 자정
- 보관 기간: 60일
- 로그 파일: `logs/my_log.log`

`logging.conf` 안의 로그 경로는 절대경로로 되어 있으므로, 다른 환경에서 실행할 경우 반드시 수정해야 합니다.

## 백테스트와 최적화

저장소에는 전략 실험용 스크립트가 포함되어 있습니다.

예시는 다음과 같습니다.

```shell
python backtest_short_term_5m.py
python backtest_eth_krw.py
python optimize_per_market.py
python validate_per_market.py
```

실거래 파라미터를 바꾸기 전에는 백테스트와 기간 분리 검증을 먼저 수행하는 것을 권장합니다.

## 최근 검증 결과

최근 30일 5분봉 백테스트에서 현재 live baseline은 아래 결과를 보였습니다.

| Market | 수익률 | MDD | PF | 거래수 | 승률 |
|---|---:|---:|---:|---:|---:|
| `KRW-XRP` | -1.32% | -2.38% | 0.2720 | 5 | 20.00% |
| `KRW-ETH` | +1.34% | -4.31% | 1.1626 | 14 | 57.14% |
| `KRW-BTC` | +10.66% | -2.17% | 98.0860 | 7 | 85.71% |

해석:

- XRP는 최근 구간에서 약세
- ETH는 소폭 플러스, 거래빈도는 중간 수준
- BTC는 09:00 일괄 청산과 3%/3% TP/SL 조합에서 가장 강한 결과

## 운영 개선 포인트

현재 코드 기준으로 우선순위가 높은 개선 포인트는 다음과 같습니다.

- 주문 UUID 기반 체결 상세 재조회
- 텔레그램 토큰 기본값 제거 및 환경 변수 강제
- API 요청 타임아웃/재시도 정책 통합
- 로그 경로를 환경 변수 또는 상대 경로 기반으로 변경
- 포지션 상태 파일과 실제 업비트 잔고 불일치 감지 강화
- 전략 파라미터를 코드가 아닌 YAML/JSON 설정 파일로 분리
- 단위 테스트와 주문 API 목킹 테스트 추가

## 보안 권장사항

- `.env`는 절대 커밋하지 마세요.
- API 키를 공유하거나 README, 이슈, 로그에 남기지 마세요.
- 출금 권한은 부여하지 마세요.
- 텔레그램 봇 토큰이 노출되면 즉시 폐기하고 새로 발급하세요.
- 서버 운영 시 SSH, 방화벽, IP 제한을 함께 적용하세요.

## 라이선스 및 책임

이 코드는 개인 자동매매 실험과 운영 보조 목적의 예제입니다. 사용자는 코드, 전략, API 키, 주문 결과, 손익에 대한 책임을 직접 부담합니다.

## 참고

- [업비트 개발자센터 API Reference](https://docs.upbit.com/reference/)
- [APScheduler Documentation](https://apscheduler.readthedocs.io/)
