# 데스크탑에서 실행

Docker Desktop과 같은 Wi-Fi의 iPhone을 사용한다. 서버는 LAN 주소로 접속하며 Apple 인증, APNs,
Deepgram, Gemini, ElevenLabs는 실제 서비스를 사용한다. 인터넷 접속이 필요하다.

`backend` 디렉터리에서 실행한다. 기존 `.env`에는 서비스 키를 보관한다.
`deploy/local.env.example`을 `.env.local`로 복사하고 데스크탑 LAN IPv4와 로컬 서비스 암호를 입력한다.
`.env.local`은 Git에서 제외된다. DB 암호와 LiveKit secret, S3 secret은 각각
`openssl rand -hex 32`로 생성한다. S3 access key와 LiveKit API key도 서로 다른 값을 사용한다.
`APNS_KEY_FILE`에는 기존 `.p8` 파일의 절대 경로를 입력한다.

```bash
docker compose --env-file .env --env-file .env.local -f docker-compose.local.yml config --quiet
docker compose --env-file .env --env-file .env.local -f docker-compose.local.yml up -d --build --wait
docker compose --env-file .env --env-file .env.local -f docker-compose.local.yml ps
curl --fail http://localhost:8080/v1/health
```

iOS Debug 앱의 API 주소는 `http://<LOCAL_HOST>:8080`으로 지정한다.
실제 기기에서 로컬 네트워크 접근을 허용한다. Apple 로그인 capability가 포함된 개발 프로파일이 필요하다.
SMS API도 유지하지만 발송하려면 SOLAPI 키와 등록된 발신번호가 필요하다.

PostgreSQL과 Redis는 컨테이너 내부에서만 접속한다. MinIO는 비공개 버킷을 자동 생성하며
앱의 파일 업로드와 다운로드에는 서명 URL을 사용한다. DB 변경은 Alembic으로 적용한다.
볼륨에는 DB와 녹음 파일이 보관된다.

같은 LAN에서 TCP 8080, 7880, 7881, 9000과 UDP 7882로 접근할 수 있어야 한다.
LiveKit은 지정한 LAN IP를 미디어 주소로 전달한다.
[LiveKit 설정 문서](https://github.com/livekit/livekit/blob/v1.13.1/config-sample.yaml)를 참고한다.
Wi-Fi가 바뀌면 `LOCAL_HOST`와 앱의 API 주소를 수정하고 위 실행 명령을 다시 실행한다.
Mac의 잠자기 중에는 통화와 API를 사용할 수 없다.
이 설정은 LAN 개발용 HTTP 환경이다. 외부 인터넷 서비스에는 기존 HTTPS 배포 설정을 사용한다.

중지할 때는 다음 명령을 사용한다. 볼륨은 유지된다.

```bash
docker compose --env-file .env --env-file .env.local -f docker-compose.local.yml down
```
