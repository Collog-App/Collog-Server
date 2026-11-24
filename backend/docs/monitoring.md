# 운영 장애 알림

GitHub Actions `Production monitor`(`.github/workflows/monitor.yml`)가 15분마다, 그리고 Actions 화면에서
수동으로 운영 서버를 확인합니다. 문제가 있으면 작업이 실패로 끝나고, 웹훅 비밀이 있으면 그 주소로 알립니다.

## 무엇을 보나

1. `https://api.collog.live/v1/health` 가 200 인지(API·DB·정기 작업). 실패하면 배포 중 잠깐 끊긴 것일
   수 있어 20초 뒤 한 번 더 보고, 그때도 실패해야 문제로 칩니다.
2. `GET /ops/api/alerts`(`X-Ops-Code` 헤더 필요)의 `ok` 가 `true` 인지. 이 주소가 보는 것과 기준은
   `app/ops.py` 의 상수입니다.

| code | 언제 | 기준 상수 |
|---|---|---|
| `LIVEKIT_UNREACHABLE` | LiveKit 서버가 API 요청에 5초 안에 답하지 않거나 오류(설정이 틀려 접속 준비를 못 한 경우 포함) | `LIVEKIT_TIMEOUT_SECONDS` |
| `ANALYSIS_BACKLOG` | 녹음된 통화가 끝나고 30분 넘게 분석 대기(`ENDED`)나 분석 중(`PROCESSING`)에 있음(건수 포함). 원음 업로드 대기 15분과 처리 제한 10분, 재시도를 넘기는 값 | `ANALYSIS_BACKLOG_AFTER` |
| `ANALYSIS_FAILING` | 분석이 끝난(완료·실패) 통화를 통화 끝난 시각 순으로 볼 때 최근 3건이 모두 실패이고, 그중 가장 최근이 24시간 안 | `FAILURE_STREAK`, `FAILURE_STREAK_WITHIN` |

응답은 `{"ok": bool, "problems": [{"code": "...", "message": "한국어 한 줄"}]}` 입니다.

감시 작업이 스스로 붙이는 종류 코드: `HEALTH_STATUS`(health 응답이 200 이 아님), `HEALTH_UNREACHABLE`,
`ALERTS_STATUS`(alerts 응답이 200 이 아님, 넘겨짐 포함), `ALERTS_UNREACHABLE`, `ALERTS_NOT_OK`, 대문자·밑줄이
아닌 서버 코드는 `UNKNOWN`.

## 알림을 보내는 때

- 문제가 있으면 작업은 매번 실패로 끝납니다. 웹훅은 **바로 앞에 끝난 실행이 성공이었을 때만** 보냅니다
  (같은 문제가 이어지는 동안 15분마다 쏟아지지 않게).
- 문제가 없는데 바로 앞 실행이 실패였으면 「경보 해제」를 한 번 보냅니다.
- 앞 실행 결과는 저장소 토큰(`GITHUB_TOKEN`, 권한 `actions: read`)으로 GitHub API 에서 같은 브랜치의 이
  작업 실행 가운데 끝난 것을 읽습니다. 읽지 못하면 문제는 놓치지 않도록 알리고, 해제 알림은 보내지
  않습니다.

## 공개 기록과 비밀

저장소가 공개라 Actions 실행 기록은 누구나 봅니다. 기록에는 문제의 종류 코드만 찍고 건수·서버 문구·응답
코드는 웹훅 본문에만 넣습니다. 운영 API 와 웹훅은 넘겨짐(리디렉트)을 따르지 않습니다(접속 코드가 다른
호스트로 따라가지 않게). 웹훅 본문에는 `"allowed_mentions": {"parse": []}` 를 넣어 글 안의 멘션이 누구도
부르지 않게 합니다. 이 동작은 `tests/test_monitor_workflow.py` 가 작업 파일의 스크립트를 꺼내 가짜
서버·웹훅으로 확인합니다.

## 켜는 법

저장소 Settings → Secrets and variables → Actions 에 두 비밀을 넣습니다. 없으면 그 단계만 건너뛰고
health 확인은 계속합니다.

| 비밀 | 값 |
|---|---|
| `OPS_MONITOR_CODE` | `monitor` 이름표 접속 코드 원문. 서버에는 SHA-256 해시만 있습니다(`docker-compose.yml` 의 `OPS_ACCESS_CODES` 기본값) |
| `ALERT_WEBHOOK_URL` | 디스코드 또는 슬랙 웹훅 주소. 본문은 `{"content": 글, "text": 글, "allowed_mentions": {"parse": []}}` 입니다(디스코드는 content, 슬랙은 text 를 읽음) |

`monitor` 이름표는 `/ops/api/alerts` 만 열 수 있고, 지표·계정 목록 같은 다른 `/ops` API 는 403 입니다.
코드를 바꾸려면 새 코드의 SHA-256 을 `OPS_ACCESS_CODES` 의 `monitor:` 자리에 넣어 배포한 뒤 비밀을 바꿉니다.

비밀 값은 작업 기록에 찍지 않습니다. GitHub 예약 작업은 몇 분 늦게 돌 수 있고, 저장소에 60일 동안 활동이
없으면 GitHub 가 예약 실행을 멈춥니다(Actions 화면에서 다시 켬).
