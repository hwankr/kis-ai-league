# kis-ai-league

2026 한투모투배틀 AI리그의 대시보드·백엔드·전략·운영 기록을 관리합니다.

기본 브랜치: `main`. Python 3.11+ API·SQLite와 React·TypeScript·Vite 기반 로컬 대시보드입니다. 외부 Python 패키지는 필요하지 않으며, 화면 빌드에는 Node.js 24 LTS를 권장합니다.

| 문서 | 내용 |
| --- | --- |
| [AI리그 핵심 규정](docs/competition-rules.md) | 참가 조건, 수상 요건, 거래 제한, 일정, 확인이 필요한 조항 |
| [프로젝트 기록](docs/project-log.md) | 결정·실험·운영 결과 |

시간 기준: `Asia/Seoul`. API 키·계좌번호·개인 증빙은 Git에 저장하지 않습니다.

`config.local.toml`에 모의투자 키를 입력합니다. 잔고 조회에는 모의 계좌 앞 8자리와 뒤 2자리도 필요합니다. 새 환경에서는 `config.example.toml`을 복사하고 `python -m venv .venv`로 준비합니다.

```powershell
npm ci
npm run build
.\.venv\Scripts\python.exe -X utf8 -m backend.dashboard
```

`npm ci`는 최초 설치·의존성 변경 시, `npm run build`는 화면 소스 변경 후 실행합니다. Python 서버가 `frontend/dist/`를 제공하므로 평소 실행에는 별도 Node 서버가 필요하지 않습니다.

화면 개발 시 Python 서버를 켜 둔 채 별도 터미널에서 `npm run dev`를 실행하고 [개발 화면](http://127.0.0.1:5173)을 엽니다. `/api`는 기존 8765 서버로 전달됩니다. React 소스는 `frontend/src/`, 로컬 폰트는 `frontend/public/fonts/`에 있습니다.

[계좌 대시보드](http://127.0.0.1:8765)에서 총평가액·예수금·유가평가액·보유 종목의 평가손익을 조회합니다. 30초 자동 갱신과 수동 새로고침을 지원하며, 실패 시 마지막 정상 조회값과 시각을 유지합니다. 평가손익은 대회 수익률이나 실현손익이 아닙니다.

대시보드의 새 조회가 성공하면 조회 시각·총자산·예수금·보유 종목을 `.local/account-history.sqlite3`에 저장합니다. 캐시 응답·조회 실패는 추가하지 않으며, 같은 금액이어도 새 조회는 기록합니다. 선택한 계좌의 최근 2,000건을 자산 변화 그래프로 표시하고 전체 기록은 로컬에 유지합니다. 화면을 닫으면 자동 수집하지 않습니다.

이력은 프로필 ID·키·계좌번호 조합별로 분리됩니다. 키(시크릿 포함)나 계좌번호를 바꾸면 기존 이력과 분리하고, 표시 이름만 바꾸면 이어집니다. 서버 재시작 후에도 이력은 유지되며, DB 파일은 Git과 HTTP 공개 대상에서 제외됩니다.

거래 내역은 기본 최근 30일, 한 번에 최대 90일을 조회합니다(앱 제한, KST). 같은 SQLite와 계좌 분리 기준을 사용하며 주문일·종목·매수/매도·누적 체결수량·평균 체결가를 표시합니다. 전체 페이지 조회 후 주문일·주문채번지점·주문번호별로 갱신하여 반복 조회·부분체결을 중복 합산하지 않습니다. 응답에서 빠진 기존 주문은 보존합니다. 조회·저장 실패는 빈 결과와 구분하고 마지막 저장값을 유지합니다. 잔고와 별도로 수동·30초 자동 갱신합니다.

체결 API 근거: [KIS 공식 조회 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_ccld/inquire_daily_ccld.py)의 모의 TR `VTTC0081R`·`VTSC9215R`, 15건/페이지·연속조회 키를 사용합니다. [3개월 월 경계](https://github.com/koreainvestment/open-trading-api/blob/main/legacy/Sample01/kis_domstk.py)를 넘으면 구간을 나눠 조회합니다. [응답 필드](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_ccld/chk_inquire_daily_ccld.py)는 주문별 누적 집계이며 개별 체결 시각·가격 목록은 제공하지 않습니다.

계좌 추가·전환:

1. [설정 예시](config.example.toml)의 `accounts.competition` 블록을 `config.local.toml` 끝에 추가하고 발급받은 키·계좌번호를 입력합니다. 기존 일반 모의투자 설정은 그대로 사용할 수 있습니다.
2. 대시보드 **새로고침 → 계좌 선택**. 서버 재시작 없이 반영하며, 마지막 선택을 브라우저에 기억합니다. 미완성 계좌는 선택할 수 없습니다.

계좌별 잔고·조회 상태를 분리하며, 전환 중에는 이전 계좌 값을 비웁니다. 로컬 PC의 모의 서버 조회만 지원하고 주문 기능은 없습니다. 대회 계좌의 실제 인증·조회는 발급 후 확인합니다. 포트가 사용 중이면 `--port 8766`을 붙입니다. 종료는 `Ctrl+C`입니다.

시세 수집은 별도 터미널에서 실행합니다. 브라우저·대시보드 서버를 닫아도 수집 프로세스가 켜져 있으면 계속 기록합니다. PC 종료·절전 중에는 수집하지 않으며 자동 시작은 등록하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -m backend.collector
# 다른 터미널에서 상태 확인·종료
.\.venv\Scripts\python.exe -X utf8 -m backend.collector --status
.\.venv\Scripts\python.exe -X utf8 -m backend.collector --stop
```

한 번만 수집하려면 `--once`를 붙입니다. 같은 저장소의 중복 실행은 거부하며 종료 요청은 진행 중인 API 요청이 끝난 뒤 완료됩니다. 기본 설정은 005930·60초입니다. [설정 예시](config.example.toml)의 `market_data`에서 종목 1~20개와 주기 10~3600초를 지정합니다. 한 회차를 마친 뒤 지정한 시간만큼 대기하며, 설정 변경은 다음 회차에 반영합니다. `account`를 생략하면 기본 계좌의 키를 사용합니다.

현재가·전일 대비율·누적 거래량·누적 거래대금·조회 시각을 `.local/market-history.sqlite3`에 저장합니다. 시세는 계좌 선택과 무관하게 출처·시장·종목별로 공유하며, 민감한 인증값은 저장하지 않습니다. 성공한 새 조회는 가격이 같아도 기록하고 같은 저장 ID의 재시도는 중복 제외합니다. 실패 시 시세를 추가하지 않고 마지막 값과 오류를 유지합니다. 대시보드의 **시세 수집** 영역은 저장값과 수집기 상태만 읽으며 직접 수집하지 않습니다. 수집기 상태가 20초 넘게 갱신되지 않으면 수집 지연으로 표시합니다.

[KIS 현재가 공식 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_price/inquire_price.py)의 모의 API `FHKST01010100`·KRX를 사용합니다. [거래량·거래대금 필드](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_price/chk_inquire_price.py)는 누적값이므로 조회별로 합산하지 않습니다. 이 API에는 거래일·체결시각이 없어 로컬 조회 시각만 기록합니다. 휴장 중에도 마지막 시세가 반환될 수 있으며, 저장값은 개별 체결이나 분봉이 아닙니다.

종목명은 [KIS 공식 코스피·코스닥 종목 파일](https://github.com/koreainvestment/open-trading-api/tree/main/stocks_info)을 `.local/stock-names.json`에 보관해 표시합니다. 24시간 지난 목록은 화면 조회 시 백그라운드에서 갱신하며 시세 API를 추가 호출하지 않습니다. 갱신 실패 시 이전 이름을 유지하고, 이름이 없는 종목은 코드로 표시합니다.

CLI:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m backend.kis check
.\.venv\Scripts\python.exe -X utf8 -m backend.kis auth
.\.venv\Scripts\python.exe -X utf8 -m backend.kis quote 005930
.\.venv\Scripts\python.exe -X utf8 -m backend.kis balance
.\.venv\Scripts\python.exe -X utf8 -m backend.kis --account competition balance
```

`check`는 로컬 설정만 확인합니다. 인증 토큰은 키별로 `.local/`에 분리해 저장·재사용합니다. API 기준: [KIS 공식 예제](https://github.com/koreainvestment/open-trading-api).

검증: `npm test`(프런트 회귀 테스트), `npm run build`(타입 검사·빌드), `.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests`(Python 오프라인 테스트).
