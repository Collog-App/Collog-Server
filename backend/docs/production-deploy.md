# 운영 배포

`docker-compose.yml`은 PostgreSQL, Redis, LiveKit, Egress, API, Caddy를 실행합니다.
오디오는 별도로 준비한 비공개 S3 호환 버킷에 저장합니다.
API와 스토리지는 HTTPS, LiveKit 신호는 WSS를 사용합니다. 개발용 고정 키와 mock은 차단됩니다.

## 준비

- 공인 IP가 있는 Linux 호스트와 Docker Compose v2가 필요합니다.
- API, LiveKit 도메인의 A 레코드를 호스트 공인 IP로 지정합니다.
- 인바운드 TCP 80, 443, 7881과 UDP 443, 7882를 허용합니다.
- PostgreSQL, Redis, API 내부 포트는 인터넷에 노출하지 마세요.
- 방화벽이 UDP와 TCP 7881을 모두 차단한 네트워크는 별도 TURN 배포가 필요합니다.

새 호스트의 `backend` 디렉터리에서 설정 파일을 준비합니다.

```bash
cp deploy/production.env.example .env
chmod 600 .env
openssl rand -hex 32
```

생성 명령을 반복해 DB_PASSWORD, JWT_SECRET, LIVEKIT_API_SECRET에 서로 다른 값을 넣습니다.
DB_PASSWORD는 URL과 설정 파일에 안전하게 사용할 수 있도록 16진수로 지정합니다.
LIVEKIT_API_KEY에도 별도의 영문·숫자 값을 지정합니다.
도메인에는 프로토콜과 경로를 넣지 마세요.

S3_ENDPOINT_URL과 S3_PUBLIC_ENDPOINT_URL에는 HTTPS S3 API 주소를 넣습니다.
일반적인 공개 S3 서비스에서는 두 값이 같습니다. S3_REGION과 S3_BUCKET을 실제 버킷 값으로 지정합니다.
스토리지 서비스에서 발급한 access key와 secret key를 사용하고 대상 버킷의 객체 생성·조회·삭제 권한을 부여합니다.
공개 읽기는 차단하고 버전 관리는 비활성화하세요. 버전 관리가 켜져 있으면 삭제 후에도 원본 버전이 남습니다.
애플리케이션은 분석 후 오디오를 삭제합니다. 장애 대비 수명 주기 만료 정책도 버킷에 설정하세요.

Deepgram 및 Gemini API 키를 설정합니다.
Apple 로그인은 기본으로 활성화되며 APPLE_CLIENT_ID에는 앱 Bundle ID를 지정합니다.
SMS 인증 API도 유지합니다. 문자를 발송할 때 SOLAPI API 키와 등록된 발신 번호를 설정합니다.
Apple 로그인을 사용할 때는 SOLAPI 설정을 모두 비워둘 수 있습니다.
APNS_KEY_FILE은 호스트의 `.p8` 절대 경로이며 컨테이너 UID 10001이 읽을 수 있어야 합니다.
개발 서명 빌드는 sandbox, TestFlight와 App Store 빌드는 production을 사용합니다.
QUESTION_TTS_PROVIDER는 기기의 한국어 음성 합성을 쓰는 ios_local 또는 elevenlabs입니다.
elevenlabs를 선택하면 API 키와 voice ID도 필요합니다.

앱의 설정 화면에서 API 주소를 배포한 HTTPS 주소로 지정합니다.
기존 로그인 토큰은 새 세션 방식에서 사용할 수 없으므로 다시 로그인해야 합니다.
Apple 계정은 Apple 사용자 식별자로 관리하며 기존 SMS 계정과 별도로 생성합니다.
기기 등록은 VoIP 토큰과 일반 알림 토큰을 따로 저장합니다.
로그아웃하면 해당 기기의 토큰을 제거하며 다른 기기의 로그인은 유지합니다.

## 실행

```bash
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=100 migrate backend egress
```

필수 값이 비어 있으면 Compose 검증이 실패합니다. Caddy는 공개 도메인의 TLS 인증서를 자동 관리합니다.
처음 실행할 때 `migrate`가 빈 DB에 테이블을 생성한 후 API를 시작합니다.
`migrate`는 성공 후 종료되는 작업이므로 Exited 0이 정상입니다.

```bash
curl --fail https://api.example.com/v1/health
```

헬스 응답만으로 실제 통화 검증을 대신할 수는 없습니다.
실기기 2대에서 SMS 로그인, 초대 수락, 백그라운드 수신, 양방향 오디오, 종료 후 분석을 확인하세요.
APNs, SMS, STT, LLM 유료 호출은 이 단계에서 발생합니다.
기침 탐지 검증 설정은 false이며 미측정 상태를 유지합니다.

공급자별 접속은 다음 명령으로 확인합니다. 키와 응답 본문은 출력하지 않습니다.

```bash
docker compose exec backend python scripts/check_providers.py
docker compose exec backend python scripts/check_apns.py --environment sandbox
```

Gemini는 합성 문장 추출, Deepgram은 무음 WAV 요청을 확인합니다.
이 검사만으로 한국어 전사 정확도를 확인할 수는 없습니다.
SMS와 APNs는 실제 사용자에게 메시지를 보내지 않으므로 기기 검증을 따로 진행해야 합니다.
리포트 알림은 분석이 완료된 통화의 참여자에게 발송하며 건강 내용은 포함하지 않습니다.
일부 기기만 발송에 실패하면 재시도 중 이미 받은 기기에 같은 알림이 도착할 수 있습니다.

## 기존 DB와 업데이트

업데이트 전 PostgreSQL 백업을 보관합니다.

```bash
docker compose exec -T postgres pg_dump -U collog -d collog -Fc > collog-before-upgrade.dump
docker compose build
docker compose run --rm migrate
docker compose up -d
```

첫 migration은 기존 테이블의 컬럼, 타입, null 허용 여부, 기본 키, 고유 제약, 외래 키를 검사합니다.
호환되면 기존 데이터를 보존하고 revision을 기록합니다.
이후 refresh 세션 테이블, 통화 관리 컬럼, 기기별 알림 설정을 추가합니다. 기존 기기의 알림은 켜진 상태를 유지합니다.
호환되지 않으면 오류를 내고 중단합니다. 스키마 자동 초기화와 파괴적인 downgrade는 허용하지 않습니다.
실패 시 오류를 검토하고 해당 DB 버전에 맞는 migration을 작성하세요.
서비스 볼륨을 삭제하거나 `SCHEMA_AUTO_RESET=true`로 우회하지 마세요.

DB_PASSWORD와 S3 키를 바꾸려면 기존 데이터 서비스의 자격 증명도 별도로 갱신해야 합니다.
환경 파일 변경만으로 PostgreSQL의 기존 계정 비밀번호가 바뀌지는 않습니다.
백업 파일은 접근을 제한하고 별도 저장소에 보관하세요.

## 자격 증명 없이 기동 검사

Docker Compose 2.24.4 이상에서 다음 명령을 실행합니다.

```bash
docker build -t collog-deploy-check:local .
uv run python scripts/check_stack.py
```

검사 스크립트는 임시 키와 DB, 별도 Docker 프로젝트를 사용하고 호스트 포트는 비공개로 유지합니다.
기존 `.env`는 사용하지 않고 실행 후 검사 컨테이너와 네트워크를 제거합니다.
전체 서비스의 healthy 상태, migration과 모델 일치 여부, Caddy를 통한 API 응답을 확인합니다.
공개 TLS 발급과 실제 공급자 API 호출, 실기기 통화는 별도로 검사해야 합니다.

## 이전 개발 설정

`deploy/livekit*.yaml`과 `deploy/egress.yaml`은 이전 로컬 실험용 파일입니다.
현재 Compose는 이 파일을 사용하지 않습니다. 운영 설정은 Compose의 환경 변수로 전달합니다.
기존 `cloud-deploy.md`의 development 모드, 고정 키, 직접 포트 노출 절차를 운영 배포에 사용하지 마세요.

공식 문서는 [LiveKit Egress](https://docs.livekit.io/transport/self-hosting/egress/)와
[Alembic async migration](https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic)을 참고하세요.
