# kis-ai-league

2026 한투모투배틀 AI리그의 대시보드·백엔드·전략·운영 기록을 관리합니다.

기본 브랜치: `main`. Python 3.11+ 기반 모의투자 조회 CLI와 로컬 계좌 대시보드입니다. 외부 Python 패키지는 필요하지 않습니다.

| 문서 | 내용 |
| --- | --- |
| [AI리그 핵심 규정](docs/competition-rules.md) | 참가 조건, 수상 요건, 거래 제한, 일정, 확인이 필요한 조항 |
| [프로젝트 기록](docs/project-log.md) | 결정·실험·운영 결과 |

시간 기준: `Asia/Seoul`. API 키·계좌번호·개인 증빙은 Git에 저장하지 않습니다.

`config.local.toml`에 모의투자 키를 입력합니다. 잔고 조회에는 모의 계좌 앞 8자리와 뒤 2자리도 필요합니다. 새 환경에서는 `config.example.toml`을 복사하고 `python -m venv .venv`로 준비합니다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -m backend.dashboard
```

[계좌 대시보드](http://127.0.0.1:8765)에서 총평가액·예수금·유가평가액·보유 종목의 평가손익을 조회합니다. 30초 자동 갱신과 수동 새로고침을 지원하며, 실패 시 마지막 정상 조회값과 시각을 유지합니다. 평가손익은 대회 수익률이나 실현손익이 아닙니다.

계좌 추가·전환:

1. [설정 예시](config.example.toml)의 `accounts.competition` 블록을 `config.local.toml` 끝에 추가하고 발급받은 키·계좌번호를 입력합니다. 기존 일반 모의투자 설정은 그대로 사용할 수 있습니다.
2. 대시보드 **새로고침 → 계좌 선택**. 서버 재시작 없이 반영하며, 마지막 선택을 브라우저에 기억합니다. 미완성 계좌는 선택할 수 없습니다.

계좌별 잔고·조회 상태를 분리하며, 전환 중에는 이전 계좌 값을 비웁니다. 로컬 PC의 모의 서버 조회만 지원하고 주문 기능은 없습니다. 대회 계좌의 실제 인증·조회는 발급 후 확인합니다. 포트가 사용 중이면 `--port 8766`을 붙입니다. 종료는 `Ctrl+C`입니다.

CLI:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m backend.kis check
.\.venv\Scripts\python.exe -X utf8 -m backend.kis auth
.\.venv\Scripts\python.exe -X utf8 -m backend.kis quote 005930
.\.venv\Scripts\python.exe -X utf8 -m backend.kis balance
.\.venv\Scripts\python.exe -X utf8 -m backend.kis --account competition balance
```

`check`는 로컬 설정만 확인합니다. 인증 토큰은 키별로 `.local/`에 분리해 저장·재사용합니다. API 기준: [KIS 공식 예제](https://github.com/koreainvestment/open-trading-api).

오프라인 테스트: `.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests`
