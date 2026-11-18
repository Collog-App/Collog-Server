#!/usr/bin/env bash
# 심재현(자녀)과 어머니(부모) 목업 계정, 그리고 슬라이드와 같은 8주치 분석 기록을 개발 DB에 넣는다.
#
#   이번 주 통화 3건 · 32분 / 지난주 2건 · 28분 / 말씀 속도 약 207음절/분(조금씩 빨라짐)
#   건강 대화 항목: 증상(허리 불편) · 복약(혈압약) · 활동(산책) · 수면(잠드는 데 오래 걸림)
#
# 개발 전용이다. 실제 건강정보를 넣지 않으며, APP_ENV가 production이거나 서버 주소가 로컬이
# 아니면 실행하지 않는다. 개발 OTP(000000)는 서버가 APP_ENV=test, MOCK_EXTERNAL_SERVICES=true로
# 떠 있을 때만 동작하므로 먼저 그렇게 서버(uvicorn 또는 docker compose)를 띄워 두어야 한다.
#
# 사용:  BASE_URL=http://127.0.0.1:8080 CHILD_PHONE=010XXXXXXXX scripts/seed_shim_demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
CHILD_PHONE="${CHILD_PHONE:-01000000002}"   # 심재현 계정으로 쓸 번호 (개발 OTP 000000)
PARENT_PHONE="${PARENT_PHONE:-01000000010}"
PY=(python)
command -v uv >/dev/null 2>&1 && PY=(uv run python)

case "$BASE_URL" in
  http://127.0.0.1:*|http://localhost:*) ;;
  *) echo "중단: BASE_URL이 로컬이 아닙니다 ($BASE_URL). 이 스크립트는 개발 환경 전용입니다." >&2; exit 1 ;;
esac
"${PY[@]}" - <<'PYEOF'
import sys
from app.config import Settings
s = Settings()
if s.app_env == "production":
    sys.exit("중단: APP_ENV=production 입니다. 개발·테스트 환경에서만 실행할 수 있습니다.")
PYEOF

"${PY[@]}" scripts/seed_demo_family.py \
  --base-url "$BASE_URL" \
  --child-phone "$CHILD_PHONE" --child-name "심재현" \
  --parent-phone "$PARENT_PHONE" --parent-name "어머니" --relation MOTHER \
  --extra-member-name "" \
  --conditions HYPERTENSION

"${PY[@]}" scripts/seed_narrative_history.py \
  --data scripts/data/shim_demo_history.json \
  --allow-same-week \
  --child-phone "$CHILD_PHONE" --parent-phone "$PARENT_PHONE"

echo
echo "완료. 앱에서 ${CHILD_PHONE} / 개발 OTP 000000 으로 로그인하면 심재현 계정에서 어머니 리포트를 볼 수 있습니다."
