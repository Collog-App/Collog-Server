# 운영 장애 알림

GitHub Actions `Production monitor`(`.github/workflows/monitor.yml`)가 15분마다, 그리고 Actions 화면에서
수동으로 운영 서버를 확인합니다. 문제가 있으면 작업이 실패로 끝나고, 웹훅 비밀이 있으면 그 주소로 알립니다.

## 무엇을 보나

1. `https://api.collog.live/v1/health` 가 200 인지(API·DB·정기 작업).
2. `GET /ops/api/alerts`(`X-Ops-Code` 헤더 필요)의 `ok` 가 `true` 인지. 이 주소가 보는 것과 기준은
   `app/ops.py` 의 상수입니다.

| code | 언제 | 기준 상수 |
|---|---|---|
| `LIVEKIT_UNREACHABLE` | LiveKit 서버가 API 요청에 5초 안에 답하지 않거나 오류 | `LIVEKIT_TIMEOUT_SECONDS` |
| `ANALYSIS_BACKLOG` | 녹음된 통화가 끝나고 20분 넘게 분석 대기(`ENDED`)나 분석 중(`PROCESSING`)에 있음(건수 포함) | `ANALYSIS_BACKLOG_AFTER` |
| `ANALYSIS_FAILING` | 분석이 끝난(완료·실패) 통화를 통화 끝난 시각 순으로 볼 때 최근 3건이 모두 실패이고, 그중 가장 최근이 24시간 안 | `FAILURE_STREAK`, `FAILURE_STREAK_WITHIN` |

응답은 `{"ok": bool, "problems": [{"code": "...", "message": "한국어 한 줄"}]}` 입니다.

## 켜는 법

저장소 Settings → Secrets and variables → Actions 에 두 비밀을 넣습니다. 없으면 그 단계만 건너뛰고
health 확인은 계속합니다.

| 비밀 | 값 |
|---|---|
| `OPS_MONITOR_CODE` | `monitor` 이름표 접속 코드 원문. 서버에는 SHA-256 해시만 있습니다(`docker-compose.yml` 의 `OPS_ACCESS_CODES` 기본값) |
| `ALERT_WEBHOOK_URL` | 디스코드 또는 슬랙 웹훅 주소. 본문은 `{"content": 글, "text": 글}` 로 둘 다 받습니다 |

`monitor` 이름표는 `/ops/api/alerts` 만 열 수 있고, 지표·계정 목록 같은 다른 `/ops` API 는 403 입니다.
코드를 바꾸려면 새 코드의 SHA-256 을 `OPS_ACCESS_CODES` 의 `monitor:` 자리에 넣어 배포한 뒤 비밀을 바꿉니다.

비밀 값은 작업 기록에 찍지 않습니다. GitHub 예약 작업은 몇 분 늦게 돌 수 있고, 저장소에 60일 동안 활동이
없으면 GitHub 가 예약 실행을 멈춥니다(Actions 화면에서 다시 켬).
