# kis-ai-league

2026 한투모투배틀 AI리그의 대시보드·백엔드·전략·운영 기록을 관리합니다.

기본 브랜치: `main`. Python 3.11+ 기반 일반 모의투자 조회 CLI를 준비했습니다.

| 문서 | 내용 |
| --- | --- |
| [AI리그 핵심 규정](docs/competition-rules.md) | 참가 조건, 수상 요건, 거래 제한, 일정, 확인이 필요한 조항 |
| [프로젝트 기록](docs/project-log.md) | 결정·실험·운영 결과 |

시간 기준: `Asia/Seoul`. API 키·계좌번호·개인 증빙은 Git에 저장하지 않습니다.

`config.local.toml`에 모의투자 키를 입력합니다. 잔고 조회에는 모의 계좌 앞 8자리와 뒤 2자리도 필요합니다. 새 환경에서는 `config.example.toml`을 복사하고 `python -m venv .venv`로 준비합니다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -m backend.kis check
.\.venv\Scripts\python.exe -X utf8 -m backend.kis auth
.\.venv\Scripts\python.exe -X utf8 -m backend.kis quote 005930
.\.venv\Scripts\python.exe -X utf8 -m backend.kis balance
```

`check`는 로컬 설정만 확인합니다. 인증 토큰은 `.local/`에 저장·재사용합니다. 대회 계좌 연결 방식은 발급 후 확인합니다. API 기준: [KIS 공식 예제](https://github.com/koreainvestment/open-trading-api).

오프라인 테스트: `.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests`
