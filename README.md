# BingX 실거래 봇 (live / $1050)

`Bingx_bot_certain`(페이퍼)에서 검증된 전략 엔진을 **실제 BingX 주문**으로 돌리는 인스턴스.
페이퍼 봇과 코드 대부분을 공유하되, 주문 집행 계층(`paper_trader.py`의 `_live_*`,
`bingx_api.py`)과 자본/리스크 설정만 실거래용으로 분리돼 있다.

## 실행

```bash
pip install -r requirements.txt
cp .env.example .env        # BINGX_API_KEY / BINGX_SECRET_KEY 채우기
python main.py              # 메뉴 시작
```

무창(백그라운드) 기동 — 워치독이 봇을 감시·자동재시작한다:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_silent.ps1
```

긴급 전량 청산:

```bash
python close_all.py
```

## 구성

| 파일 | 역할 |
|---|---|
| `main.py` | 진입점. TTY면 시작 메뉴, 백그라운드면 자동 이어하기 |
| `strategy_engine.py` | 스캔·진입·청산·DCA 판단 (전략 본체) |
| `paper_trader.py` | 포지션 회계 + 실거래 주문 집행(`_live_entry/_live_close/_live_add`) |
| `bingx_api.py` | BingX REST 래퍼 (서명·잔고·시세·주문·포지션) |
| `coin_scanner.py` | 후보 코인 스캔/점수화 |
| `config.py` | 전 파라미터 |
| `watchdog.py` | 봇 감시·자동재시작 (네트워크 단절 대응) |
| `health_check.ps1` | 워치독 생존 확인(전역 뮤텍스 기준) 후 재기동 |

## 실거래 설정 요점 ($1050 기준)

| 항목 | 값 |
|---|---|
| `LIVE_TRADING` | `True` — 실제 주문 집행 |
| 총 자본 | $1,050 |
| 레버리지 | 8x |
| 진입 시드 | $60 (명목 $480) — 자본 연동 동적 계산 |
| DCA | 최대 4단계, 누적 상한 시드×16 = $960 |
| 1회 손절 상한 | `min(시드×4, $250)` = $240 |
| 일일 방향 손실 한도 | -$200 → 해당 방향 차단 |
| 일일 총 손실 한도 | -$250 → 60분 매매 중단 |
| 자동 출금 | 비활성 (실거래에선 장부만 어긋남) |

## 실거래 안전장치

- **진입 전 미청산 포지션 검사** — 거래소에 남은 포지션이 있으면 신규 진입 차단
- **주문 실패 시 장부 미반영** — 진입 주문 거부되면 내부 상태를 만들지 않음
- **청산 실패 시 상태 유지 + CRITICAL 로그** — 포지션을 잃어버리지 않도록 IDLE 전환 금지
- **부분 DCA** — 가용 마진이 계획 금액보다 적으면 마진의 90%까지만 투입
- **워치독 뮤텍스 분리** (`BingX_Phoenix_Live_v1`) — 페이퍼 봇 인스턴스와 충돌 방지

## 주의

- `.env`(API 키), `state.json`, `*.log`는 `.gitignore`로 제외된다. 커밋하지 말 것.
- BingX **헤지 모드**(LONG/SHORT 동시 보유) 계정 설정이 필요하다.
- 실제 자금이 움직인다. 파라미터 변경 후에는 반드시 `close_all.py` 사용법을 먼저 확인할 것.
