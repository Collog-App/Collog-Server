### 콜록 Server

https://testflight.apple.com/join/6DEKfQMw

> 멋쟁이사자처럼대학 14기 중앙해커톤 AAC 트랙 128팀 중 **2위** <br />
> 멋쟁이사자처럼대학 14기 중앙해커톤 317팀 중 **장려상**

![thumbnail](https://github.com/user-attachments/assets/0adbf701-3903-4b8a-9939-e91c1984c5e3)
![likelion](https://github.com/user-attachments/assets/d7b19b68-10b4-445d-9b29-db35b93575ae)

```mermaid
flowchart LR
    IOS(["Collog-iOS<br/>CallKit, PushKit"])
    API["Collog-Server<br/>Python, FastAPI<br/>SQLAlchemy, librosa"]
    MEDIA["LiveKit<br/>Egress, Redis"]
    STORAGE[("MinIO<br/>Audio storage")]
    DB[("PostgreSQL<br/>Alembic")]
    AI["Deepgram, Gemini<br/>Transcription, extraction"]
    TTS["ElevenLabs<br/>Text to speech"]

    IOS <-->|REST API, TTS tokens| API
    API -.->|APNs push| IOS
    IOS <-->|WebRTC| MEDIA
    IOS <-->|Speech streaming| TTS
    IOS -->|PCM upload| STORAGE
    MEDIA -->|Recordings| STORAGE
    API <-->|Audio access| STORAGE
    API --> DB
    API <-->|Analysis requests| AI

    classDef app fill:#14532d,stroke:#14532d,color:#fff
    classDef server fill:#dcfce7,stroke:#22c55e,color:#14532d
    classDef service fill:#eff6ff,stroke:#93c5fd,color:#1e3a8a
    classDef data fill:#f8fafc,stroke:#94a3b8,color:#334155
    class IOS app
    class API server
    class MEDIA,AI,TTS service
    class DB,STORAGE data
```

### 실행

```bash
cd backend
docker compose --env-file .env --env-file .env.local -f docker-compose.local.yml up -d --build --wait
```

### 문서

- [외부 서버 배포](backend/docs/production-deploy.md)
- [통화 처리](backend/docs/ios-call-flow.md)
- [전사와 정보 추출](backend/docs/ai-transcript-design.md)
- [음향 분석](backend/docs/acoustic-design.md)

<br />
<sub>
© 2026 Team Raichu of LIKELION SeoulTech. All rights reserved.
</sub>
