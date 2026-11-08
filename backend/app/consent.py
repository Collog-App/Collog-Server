CONSENT_VERSION = "2026-10-26.v5"
CONSENT_ITEMS = [
    "SENSITIVE_HEALTH_COLLECTION",
    "VOICE_FEATURE_EXTRACTION",
    "CALL_RECORDING",
    "REPORT_SHARING_WITH_CHILD",
    "THIRD_PARTY_AI_PROCESSING",
]

# 건강 앱 연결은 위 필수 동의와 별개인 선택 동의다. 앱은 이 문서를 그대로 보여 준다.
HEALTH_LINK_VERSION = "health-link-2026-11-08.v1"
HEALTH_LINK_TITLE = "건강 앱 연결 (선택)"
HEALTH_LINK_ITEMS = [
    "휴대폰 건강 앱(Health Connect·Apple 건강)에 기록된 수면 시간, 걸음 수, 심박, 혈압, 혈당을 "
    "하루 단위로 요약해 콜록 서버로 보냅니다. 분 단위 원본 기록은 보내지 않습니다.",
    "요약은 가족 건강 리포트에 생활 변화를 보여 주는 데만 씁니다.",
    "리포트 공유를 허락한 가족 구성원에게만 보입니다.",
    "연결을 끊거나 탈퇴하면 서버에 저장된 요약을 바로 지웁니다.",
    "동의하지 않아도 콜록의 다른 기능은 그대로 쓸 수 있습니다.",
    "이 정보는 의료 진단이 아니며 건강 변화 참고용입니다.",
]
