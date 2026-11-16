# AI-2 실제 음향 지표 설계

상태: hackathon prototype analyzer 구현 완료, 기침 labeled validation·실기기 보정 필요

## 1. 목표와 금지선

부모의 개인 기준선과 비교할 네 관찰값만 계산한다.

- `SPEECH_RATE`: 발화 속도
- `PAUSE_RATIO`: 발화 내부 휴지 비율
- `F0_VARIATION`: 기본주파수의 robust 변동 폭
- `COUGH_EVENTS`: 기침 후보 event 수

이 값으로 질환, 위험군, 응급도나 원인을 판정하지 않는다. 품질이 부족하면 수치를 만들지 않고
`UNMEASURABLE`과 이유를 저장한다.

## 2. 입력과 실행 위치

```text
iOS 48 kHz mono PCM WAV
  ├─ 품질 검사
  ├─ 16 kHz float32 정규화 ── pYIN / transient candidate detector
  ├─ 부모 Deepgram word·utterance timing ── 속도 / 휴지
  └─ voiced frame pYIN ── F0 variation
```

- F0와 기침은 iOS `DEVICE_RAW`만 사용한다. Opus Egress fallback은 codec/AEC 영향 때문에
  해당 두 지표를 `UNMEASURABLE(SOURCE_NOT_RAW)`로 둔다.
- 발화 속도와 휴지는 부모 Track Egress의 Deepgram timing을 사용할 수 있다.
- 48 kHz 원본은 decode 직후 메모리에서만 다루고 VAD/YAMNet용으로 16 kHz mono float32로
  resample한다.
- 현재 CPU 분석은 event loop를 막지 않도록 worker thread에서 실행한다. durable Redis
  `analysis-worker` container 분리는 production 후속 작업이다.
- 최종 특징값과 model/rule version을 저장한 뒤 원본 PCM을 폐기한다.

## 3. 공통 품질 게이트

아래 중 하나라도 실패하면 관련 지표는 `UNMEASURABLE`이다.

| 검사 | 초기 기준 | 실패 이유 |
|---|---:|---|
| WAV decode / mono PCM | 성공 | `INVALID_AUDIO` |
| sample rate | 16/24/44.1/48 kHz 입력 후 16 kHz 변환 가능 | `UNSUPPORTED_SAMPLE_RATE` |
| 부모 발화 | 20초 이상 | `INSUFFICIENT_PARENT_SPEECH` |
| clipping | `abs(sample) >= 0.999` frame 비율 1% 미만 | `EXCESSIVE_CLIPPING` |
| 유효 음량 | speech RMS가 -45 dBFS 초과 | `SIGNAL_TOO_QUIET` |
| 유효 pYIN frame | 2초 이상 | `INSUFFICIENT_VOICED_AUDIO` |

threshold는 해커톤 fixture로 고정하지 않고 실기기 20~30통의 분포를 본 뒤 versioned config로
확정한다.

## 4. 지표별 정의

### 4-1. 발화 속도 `SPEECH_RATE`

한국어 STT text에서 한글 음절 블록(`[가-힣]`) 수를 세고, 부모 utterance의 실제 발화 구간으로
나눈다.

```text
speech_rate = 60 × hangul_syllable_count / articulation_seconds
unit = syllables_per_minute
```

`articulation_seconds`는 각 utterance에서 300ms 이상인 내부 pause를 제외한 시간이다. 최소
10음절·5초 articulation이 필요하다. STT confidence가 낮은 단어는 분자에서 제외하며, 같은
STT model/version끼리만 기준선을 비교한다.

### 4-2. 휴지 비율 `PAUSE_RATIO`

전체 통화 침묵을 사용하면 자녀 말을 듣는 시간이 pause로 잘못 계산된다. 반대로 utterance
안의 간격만 세면 Deepgram이 쉬는 지점에서 utterance를 나누기 때문에 값이 0이 된다(v5까지).
v6부터는 자녀 발화가 끼지 않고 이어진 부모 utterance들을 한 묶음(run)으로 본다.

```text
run          = 부모 utterance 연속, 사이 gap ≤ 2000ms 이고 그 gap에 자녀 utterance가 겹치지 않음
pause        = run 안의 연속 word gap in [300ms, 2000ms] (utterance 경계 포함)
pause_ratio  = sum(pause) / sum(run first-word → last-word window) × 100
unit         = %
```

2초보다 긴 gap과 자녀가 말한 gap은 turn-taking으로 보고 제외한다. 자녀 트랙이 없으면 이를
구분할 수 없으므로 `UNMEASURABLE(NO_CHILD_TRACK)`, 단어 2개 이상인 run이 없으면
`UNMEASURABLE(INSUFFICIENT_WORD_TIMING)`이다. 턴 전환 지연(자녀 발화 끝 → 부모 응답)은 이
지표에 넣지 않는다.

### 4-3. 기본주파수 변동 `F0_VARIATION`

raw PCM을 16 kHz로 변환하고 `librosa.pyin`으로 F0와 voiced probability를 구한다. 초기 범위는
65~400 Hz이며 voiced probability 0.8 이상 frame만 사용한다.

절대 Hz 표준편차 대신 개인의 평균 pitch에 덜 민감한 semitone robust spread를 사용한다.

```text
semitone_i = 12 × log2(f0_i / median(f0))
f0_variation = 1.4826 × median(abs(semitone_i - median(semitone)))
unit = semitone_mad
```

유효 F0 frame이 2초 미만이거나 octave jump가 과도하면 `UNMEASURABLE(PITCH_UNSTABLE)`이다.

### 4-4. 기침 후보 event `COUGH_EVENTS`

**현재 상태: `UNMEASURABLE(DETECTOR_NOT_VALIDATED)` 고정.** `Settings.cough_detector_validated`
기본값이 `False`이며, `analyze`가 계산 경로와 무관하게 기침 결과를 덮어쓴다.

`transient-heuristic-v1`은 16 kHz mono waveform을 0.96초 patch/0.48초 hop으로 나눠 상대 에너지
상승, 1 kHz 이상 power 비율, zero-crossing rate, crest factor를 가중합하고 threshold 이상 인접
patch를 750ms 기준으로 병합한다. 2026-08-13 공개 도메인 라벨 음원(기침 4, hard negative 4)으로
측정한 결과 이 detector는 사용할 수 없다.

모델 선정 근거와 유사 서비스 비교는 `voice-health-model-research.md`에 있다.

| 항 | 가중치 | 실측 결과 |
|---|---:|---|
| energy | 0.40 | 기준선을 클립 자신의 median으로 잡아 5개 중 4개에서 0.00. 기침이 잦을수록 median이 올라가 스스로를 가린다 |
| crest | 0.15 | 단독 녹음(crest 9~18)에서는 동작하지만 발화가 프레임을 채우는 통화 조건에서 붕괴 |
| high | 0.25 | 동작하지만 웃음에 만점(1.00)을 준다. `Man_coughing`은 0.00 |
| zcr | 0.20 | 동작하지만 웃음에 만점(1.00)을 준다 |

energy가 죽어 8개 파일 최고 점수가 0.609로 threshold 0.65에 닿지 못했다. threshold를 0.40으로
낮추면 웃음이 7회로 모든 기침 파일보다 높게 잡힌다. 네 축 어디에도 기침과 웃음을 가르는 정보가
없어 상수 튜닝으로는 해결되지 않는다.

#### 교체 detector: `hear-event-detector-small-v1`

기본 detector는 HeAR health acoustic event detector(`google/hear`의 `event_detector_small`)의
ONNX 변환본으로 바꿨다. MobileNet-V3 backbone이며 2초/16 kHz mono clip마다 8 class 확률을 낸다.

```text
['Cough', 'Snore', 'Baby Cough', 'Breathe', 'Sneeze', 'Throat Clear', 'Laugh', 'Speech']
```

`Throat Clear`, `Laugh`, `Sneeze`가 별도 class라 hard negative를 무엇으로 오인했는지 구분할 수
있다. 같은 라벨 음원 8개 기준 결과다.

| 파일 | 정답 | `Cough` 최고 | 최고 class | 검출 구간 |
|---|---|---:|---|---:|
| Man_coughing | 기침 | 0.9998 | Cough | 1 |
| Cough_1 | 기침 | 1.0000 | Cough | 3 |
| Cough_2 | 기침 | 1.0000 | Cough | 3 |
| Woman_coughing_three_times | 기침 | 0.9993 | Cough | 1 |
| Sneezing | 재채기 | 0.0264 | Sneeze | 0 |
| Laughter_and_clearing_voice | 웃음+헛기침 | 0.3992 | Laugh | 0 |
| Laughter | 웃음 | 0.6880 | Laugh | 0 |
| Knocking_on_wood_or_door | 문 두드림 | 0.0008 | — | 0 |

threshold 0.9에서 기침 4/4 검출, hard negative 오탐 0/4다. clip당 약 3 ms로 60초 통화가
0.5초 hop 기준 0.4초면 끝난다. TensorFlow 없이 `onnxruntime`(71 MB)만 쓴다.

**단위가 `회`에서 `구간`으로 바뀌었다.** 2초 window는 "기침이 있는가"에는 답하지만 "몇 회인가"
에는 답하지 못한다. 기침 한 번이 앞뒤 window를 모두 양성으로 만들기 때문이다. 그래서 세는 값은
기침 횟수가 아니라 연속 검출 구간의 수이며 `cough_unit()`이 detector에 따라 단위를 정한다.
기준선은 같은 analyzer version끼리만 비교하므로 단위가 섞이지 않는다.

**여전히 `UNMEASURABLE`이다.** `cough_detector_validated` 기본값은 `False`다. 위 8개는 깨끗한
단일 음원이라 통화 조건의 precision을 대표하지 않는다. §7의 cough 30 / hard negative 30을
통과하기 전에는 값을 내보내지 않는다.

#### 모델 획득과 라이선스

가중치는 HAI-DEF 약관 대상이라 **저장소에 커밋하지 않는다.** `backend/models/`는 `.gitignore`
대상이며 각자 한 번 받는다.

```bash
HF_TOKEN=hf_... uv run --with tensorflow --with tf2onnx python scripts/fetch_cough_model.py
```

스크립트는 HF revision을 고정해 내려받고, tf2onnx로 opset 17 변환한 뒤 무음 입력으로 동작을
확인하고, `NOTICE`와 provenance JSON(revision, sha256, 도구 버전)을 남긴다. 런타임은
`cough_model_sha256`으로 파일을 검증하며 불일치 시 `MODEL_CHECKSUM_MISMATCH`, 파일이 없으면
`MODEL_UNAVAILABLE`로 떨어진다. 어느 경우에도 숫자를 만들지 않는다.

약관상 재배포 시 HAI-DEF 고지, 변경 사실 표기, 사용 제한 승계가 필요하다. 공개 저장소에
변환본을 올리지 않는 이유다.

UI에는 항상 “기침 후보 event” 또는 검수 후 “기침 event”로만 표시한다. 어떤 classifier도
의료기기용 기침 진단 model이 아니다.

## 5. 기준선과 robust Z

- 동일인·동일 time slot의 과거 값만 사용한다.
- 현재 통화는 기준선 계산에서 제외한다.
- calendar week별 median 한 개로 집계하고 최소 4개 서로 다른 주가 필요하다.
- `robust_z = 0.6745 × (current - median) / MAD`를 사용한다.
- MAD가 0이면 epsilon으로 큰 값을 만들지 않고 `UNSCORABLE_ZERO_VARIANCE`로 둔다.
- 방향은 speech rate 감소, pause/cough 증가를 별도로 기록한다. F0 variation은 증가·감소를
  관찰값으로만 보여주고 위험 방향을 단정하지 않는다.
- `4주 연속`은 네 개의 서로 다른 ISO calendar week가 모두 같은 방향이고 중간 결측 주가
  없을 때만 성립한다.

## 6. 현재 실행 계약과 후속 worker

```text
AcousticJob(callId, rawAudioUri, transcriptId, analyzerVersion)
  → measurements[
      metric, value, unit, status, reason,
      source, modelVersion, quality
    ]
```

현재 dependency:

- decode: Python PCM WAV loader, resample: `librosa` + `soxr`
- F0: `librosa.pyin`
- cough: 현재 `transient-heuristic-v1`은 validation 실패. HeAR `event_detector_small`과 YAMNet
  bake-off 후 선택 모델로 교체

model artifact는 checksum과 license를 기록하고 container image에 pin한다. 외부 inference API로
raw 건강 음성을 추가 전송하지 않는다.

## 7. 검증 계획과 합격 기준

### deterministic fixture

- 120/180/240 syllables-per-minute 합성 음성
- 0.3/0.6/1.2초 내부 pause를 넣은 음성
- 100/150/220 Hz tone과 알려진 vibrato 폭
- silence, clipping, 저음량, 깨진 WAV

합격 오차:

- speech rate ±5%
- pause ratio 절대오차 ±0.03
- F0 median ±3 Hz, variation ±0.2 semitone
- 품질 실패 fixture는 숫자 대신 정확한 `UNMEASURABLE` reason

### cough validation

실제 건강정보가 아닌 동의된 더미/공개 license 음원으로 cough 30개 이상, hard negative
30개 이상(재채기·목 가다듬기·웃음·문 닫힘)을 label한다. 이는 pipeline smoke용이다. 모델
선택용 in-domain bake-off는 5명 이상, cough bout 100개 이상, hard-negative 통화 1시간 이상을
speaker 단위로 분리한다. threshold는 calibration split에서 precision 우선으로 선택하고 최소
목표를 precision 0.85로 두되 recall과 FP/hour도 함께 공개한다. 이 기준을 못 넘으면 데모에서도
숫자를 확정값으로 표시하지 않는다.

### 실기기 검증

같은 화자가 같은 문장을 다음 조건으로 녹음한다.

- iPhone 기종 2개 이상
- 스피커/수화기, 조용한 방/생활 소음
- Wi-Fi/셀룰러 전환
- 오전/저녁

raw PCM과 Egress 결과를 비교해 source 차이를 기록하되 raw PCM만 개인 기준선의 음향값으로
채택한다.

## 8. 구현 상태와 다음 순서

1. 완료: Deepgram word timing 보존과 Transcript JSON 확장
2. 완료: 16-bit PCM WAV loader, 품질 gate, 16 kHz 변환
3. 완료: word timing speech rate/pause ratio
4. 완료: pYIN F0 variation
5. 완료: versioned transient cough 후보 detector와 deterministic fixture
6. 완료: calendar-week median, MAD=0 `UNSCORABLE`, 결측 주 연속 판정 수정
7. 완료: `transient-heuristic-v1` 실패 확인과 `COUGH_EVENTS` `UNMEASURABLE` 고정 (v4)
8. 완료: HeAR event detector 도입 — `onnxruntime` 의존성, `fetch_cough_model.py` 획득 경로,
   sha256 검증, HAI-DEF NOTICE, 단위 `회` → `구간` (v5)
9. 남음: cough 30/hard-negative 30 validation과 threshold freeze. 통과해야
   `cough_detector_validated`를 켠다
10. 남음: calibration harness(`scripts/calibrate_acoustics.py`)와 라벨 fixture
11. 남음: 실제 통화 조건(부모 발화 + Opus codec + 노년층 화자) 재측정. 현재 근거는 깨끗한
    단일 음원 8개뿐이다
12. 남음: 컨테이너 배포 시 모델 주입 경로. `Dockerfile`은 `app`과 `scripts`만 복사하므로
    이미지에 모델이 없다. `cough_detector_validated`를 켜기 전에 volume mount 또는 build
    secret으로 `HF_TOKEN`을 받아 빌드 중 획득하는 방식을 정한다
13. production 후속: 별도 Redis worker/queue와 model artifact checksum

## 참고

- [Silero VAD](https://github.com/snakers4/silero-vad): 8/16 kHz, speech timestamp와 ONNX 지원
- [librosa pYIN](https://librosa.org/doc/main/generated/librosa.pyin.html): F0, voiced flag,
  voiced probability 제공
- [TensorFlow YAMNet tutorial](https://www.tensorflow.org/hub/tutorials/yamnet): 16 kHz mono 입력과
  AudioSet 521 class score
- [Google AudioSet](https://research.google.com/audioset/): 범용 audio event ontology/dataset
- [Google HeAR event detector demo](https://github.com/Google-Health/hear/blob/master/notebooks/hear_event_detector_demo.ipynb):
  MobileNetV3 Small/Large, 8개 health event score, TFLite 변환 예제
- [HeAR PyTorch model card](https://huggingface.co/google/hear-pytorch): 2초 음원을 512차원으로
  바꾸는 ViT-L embedding model이며 cough detector 자체가 아님
- [Deepgram Utterances](https://developers.deepgram.com/docs/utterances): utterance/word timing
