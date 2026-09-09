# Conan AI — 백엔드 (be)

영상 URL을 받아 **미디어 조작 가능성**과 **주장 사실성**을 분석하는 백엔드 서비스입니다.

기획·설계 기준은 [docs 레포](https://github.com/Dynamic-Juo/docs)를 따릅니다
(`project/prd.md`, `design/ai-pipeline.md`). 작업 규칙은 [AGENTS.md](AGENTS.md)를 참고하세요.

> **[파이프라인 해부도](docs/pipeline.md)** — 단계별 흐름도, 각 단계의 라이브러리와 실패 처리,
> 모델·라이브러리를 갈아끼울 때 건드릴 곳, 구간별 실측 성능. 검증하거나 무언가를 교체할 때 여기부터 보세요.

## 세 축을 분리한다

제품은 분석 결과를 **하나의 진위 점수로 합치지 않습니다**. 실제 인물이 나온 영상에도
허위 주장이 있을 수 있고, AI로 만든 영상의 발언이 사실일 수도 있기 때문입니다. 미디어
조작도 두 가지를 구분합니다 — 얼굴을 합성·변형한 것과, 영상 전체를 AI가 만든 것은
서로 다른 문제입니다.

| 축 | 응답 필드 | 내용 |
|---|---|---|
| 얼굴 합성·변형 | `face_manipulation` | 프레임 분류기 + 휴리스틱 + 자가표기 + 선택적 VLM |
| 영상 전체 AI 생성 | `whole_video_generation` | 자가표기만 (전용 탐지 모델은 아직 없음 — docs T-06) |
| 주장 사실성 검증 | `claim_verification` | 주장 추출 → 외부 근거 검색 → 근거와 일치·불일치·근거 부족 |

미디어 조작 두 축은 **숫자 점수를 노출하지 않습니다.** `조작 의심`/`뚜렷한 조작 징후 없음`/
`판단 보류`/`분석 불가` 네 단계로만 응답합니다(`status`, 표시용 한국어 문구는 `status_label`).
내부적으로 계산하는 연속 점수는 `signals` 안에만 남아 있고 디버깅용입니다 — 확률처럼 보이는
숫자를 사용자에게 보여주면 실제보다 정밀한 판정처럼 오해될 수 있어서입니다.

또 하나의 원칙은 **"분석 못 함"과 "분석했더니 정상"을 구분**하는 것입니다. 프레임을 한 장도
못 뽑았는데 "뚜렷한 조작 징후 없음"이라고 답하면 실패가 무죄 판정으로 둔갑합니다. 그런 경우
`status`는 `unavailable`("분석 불가")입니다.

이 원칙은 얼굴 검출에도 적용됩니다. 지금 쓰는 분류기는 얼굴 crop 이미지로 학습된 모델이라,
영상에서 얼굴을 한 명도 못 찾으면 전체 프레임에 대한 점수를 근거로 쓸 수 없습니다. 이때도
낮은 점수를 "정상"으로 발표하지 않고 판단을 유보합니다.

### 판정에 대한 태도

우리가 내는 판정은 **주장의 진위가 아니라 주장과 근거의 관계**입니다. `근거와 일치`는
"이 주장은 참이다"가 아니라 "우리가 찾은 근거는 이 주장과 일치한다"는 뜻입니다.

판정은 신뢰도 순서로 시도합니다.

1. **전문 기관의 공개 판정**이 있으면 그대로 옮깁니다. 가장 신뢰도가 높습니다.
2. 없으면 **LLM에게 근거를 주고 관계를 판정**하게 합니다.
3. LLM도 못 쓰면 근거만 붙이고 판단을 유보합니다.

2번에는 환각을 막는 장치가 있습니다. LLM은 판정과 함께 **근거 원문에서 발췌한 문장**을
제출해야 하고, 서버가 그 문장이 실제 근거에 있는지 대조합니다. 없으면 판정을 통째로 버리고
`근거 부족`으로 강등합니다. 생성의 위험을 검증으로 막는 구조입니다.

- 검색 결과가 없다는 이유만으로 주장을 거짓으로 판정하지 않습니다.
- 근거가 부족하거나 서로 충돌하면 판단을 유보합니다.
- 주장과 근거의 기준 시점이 다르면 수치가 달라도 `근거와 불일치`가 아니라 `근거 부족`입니다.

LLM 없이도 동작합니다(`DEEPCHECK_LLM_PROVIDER=off`). 그때는 전문 기관 판정을 찾지 못한
모든 주장이 `근거 부족`으로 끝납니다.

## 파이프라인

```
                        ┌─► 프레임 추출 ─► 얼굴 crop ─► 딥페이크 분류 ──► 미디어 조작
URL ─► 다운로드(yt-dlp) ─┤
                        └─► 자막 있으면 자막 ─┐
                            없으면 STT ──────┴─► 텍스트 신호 ──► 미디어 조작
                                              └─► 주장 추출 ─► 근거 검색 ─► 주장 사실성
```

| 단계 | 모듈 | 도구 |
|---|---|---|
| 오케스트레이션 | `deepcheck/pipeline.py` | 단계별 상태·소요시간 기록 |
| 다운로드·자막 | `deepcheck/downloader.py` | yt-dlp + PyAV (시스템 ffmpeg 불필요) |
| 자막 파싱 | `deepcheck/captions.py` | WebVTT/SRT 직접 파싱 |
| STT | `deepcheck/transcriber.py` | faster-whisper (CTranslate2) |
| 얼굴 crop | `deepcheck/face.py` | MediaPipe |
| 조작 탐지 | `deepcheck/deepfake.py` | ViT 분류기 + 휴리스틱 + 선택적 VLM |
| 텍스트 신호 | `deepcheck/analyzer.py` | 자가표기·합성음성·클릭베이트 |
| 주장 검증 | `deepcheck/claims.py` | 규칙 기반 추출 + 위키백과·GDELT·팩트체크 API |
| 결과 조립 | `deepcheck/report.py` | 두 축 분리, text/JSON/HTML 출력 |
| API | `backend/app.py`, `backend/harness.py` | FastAPI + 워커 풀 |
| 에러·로그 | `deepcheck/errors.py`, `deepcheck/logging_setup.py` | 도메인 예외, request_id 로깅 |

**자막이 있으면 STT를 건너뜁니다.** 설계 문서의 분기를 그대로 따른 것으로, 수십 초가
걸리는 음성 인식을 통째로 아낍니다. 기본값은 사람이 단 자막만 사용하고(`manual`),
자동 생성 자막까지 쓰려면 `caption_policy: "any"`로 요청하세요.

> **한국어 영상 주의.** 자막이 없으면 2분짜리 영상에도 STT가 88초(`small`) 걸립니다.
> `tiny`는 53초로 빠르지만 "7월"을 "18월", "이창용"을 "2장용"으로 옮깁니다. 주장 추출이
> 수치와 고유명사에 기대기 때문에, 한국어에서 `tiny`를 쓰면 존재하지 않는 주장을 검증하게
> 됩니다. 속도가 필요하면 `tiny` 대신 자막이 있는 영상을 쓰세요.
> 자세한 실측은 [파이프라인 해부도](docs/pipeline.md)를 참고하세요.

## 실행

### Docker (권장)

```bash
docker compose up -d --build
curl http://localhost:8000/health
```

### 로컬

```bash
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -r requirements.txt -r requirements-vision.txt -r requirements-backend.txt
uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

CLI로도 쓸 수 있습니다.

```bash
python -m deepcheck.cli analyze "URL" --model-size tiny --json
python -m deepcheck.cli analyze "URL" --no-classifier       # 휴리스틱만
python -m deepcheck.cli analyze "URL" --save-transcript     # STT 원문 저장
```

### 테스트

```bash
uv pip install -r requirements-dev.txt
pytest
```

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/analyze` | `{url, model_size, max_frames, use_classifier, vlm_model, enable_claim_verification, session_id}` → `{job_id, session_id, deduplicated}` |
| GET | `/api/jobs/{job_id}` | `{status, progress, message, result, error}` — status 값은 아래 표 참고 |
| GET | `/api/sessions/{session_id}` | 해당 세션이 요청한 job 목록 |
| GET | `/health` | `{status, harness:{...}}` |

분석은 무거우므로 job 방식입니다. POST로 `job_id`를 받고 2~3초 간격으로 폴링하세요.
같은 URL이 이미 처리 중이면 새 job을 만들지 않고 기존 job을 재사용합니다(`deduplicated: true`).
대기열이 가득 차면 `429`를 반환하니 잠시 후 재시도하면 됩니다.

### job 상태값

`result`는 **폴링마다 조금씩 채워집니다.** 미디어 조작 두 축은 준비되는 즉시, 주장 카드는
개수가 확정되면 전부(대기 중 상태로) 먼저 나타났다가 하나씩 검증되며 갱신됩니다. 두 결과 중
어느 게 먼저 올지는 보장하지 않습니다.

| status | 의미 |
|---|---|
| `queued` | 대기열에서 워커를 기다리는 중. `result`는 비어 있다 |
| `processing:collecting` | 영상·프레임을 확보하는 중 |
| `processing:transcribing` | 발언 텍스트(자막 또는 STT)를 확보하는 중 |
| `processing:extracting_claims` | 주장을 추출하고 중복을 합치는 중 |
| `partially_completed` | 첫 부분 결과가 나왔다. `result`의 키를 보고 뭐가 준비됐는지 확인 |
| `completed` | 모든 분석이 끝났다 |
| `completed_with_limitations` | 일부 분석을 못 했지만 나머지 결과는 있다 (`stages`에서 실패한 단계 확인) |
| `timed_out` | 전체 처리 시간(기본 10분)을 넘겼다. 완료된 결과는 유지, 나머지 주장 카드는 `status: "timed_out"` |
| `failed` | 진행할 수 없는 오류. `error`에 사유 |

### result 스키마 (프론트 계약)

```jsonc
{
  "url": "...",
  "media": { "title": "...", "uploader": "...", "duration": 26, "video_id": "..." },
  "analysis_status": "complete",        // 한 단계라도 실패/건너뜀이면 "partial". job.status가 최종 상태의 기준
  "stages": {                            // 무엇을 했고 무엇을 못 했는지
    "download":  { "status": "ok", "detail": null, "elapsed_sec": 6.1 },
    "frames":    { "status": "ok", "detail": "8장 추출", "elapsed_sec": 1.2 },
    "transcript":{ "status": "ok", "detail": "24단어, 커버리지 99.5%", "elapsed_sec": 12.0 }
  },
  "face_manipulation": {
    "status": "suspected",               // suspected / no_clear_signs / inconclusive / unavailable
    "status_label": "조작 의심",          // 화면에 그대로 쓸 수 있는 한국어 문구
    "detail": null,                      // 강등되거나 판정 사유가 있으면 여기
    "evidence": ["frame_000.jpg: fake 98.4% (라벨 Fake)", "제목/설명에 자가표기 발견: \"ai generated\""],
    "signals": { "combined_risk": 63.0, "frames_analyzed": 8, "frames_with_face": 6, "..." : "..." }
    // signals는 디버그용이다. 확률처럼 보이는 숫자를 사용자에게 보여주지 않는다 — status만 표시할 것.
  },
  "whole_video_generation": {
    "status": "unavailable",
    "status_label": "분석 불가",
    "detail": "영상 전체 AI 생성 탐지 모델이 아직 선정되지 않았다.",
    "evidence": [],
    "signals": { "self_disclosure_risk": 0, "model": null }
  },
  "claim_verification": {
    "status": "analyzed",                // 또는 "unavailable"
    "summary": { "total": 3, "supported": 0, "refuted": 1, "unverified": 2 },
    "claims": [
      {
        "text": "2024년 실업률이 3.2% 감소했다",
        "start": 12.5, "end": 18.0,      // 영상에서 이 말이 나온 위치 (없으면 null)
        "mentions": [],                  // 같은 주장이 반복될 때의 문맥·횟수
        "status": "done",                // pending / verifying / done / failed / timed_out (카드 처리 상태)
        "verdict": "refuted",            // supported / refuted / unverified (status가 done일 때만 의미 있음)
        "verdict_label": "근거와 불일치",  // 화면에 그대로 쓰면 되는 문구
        "reason": "통계청 자료는 같은 기간 실업률이 올랐다고 밝히고 있다.",
        "quote": "2024년 연간 실업률은 전년 대비 0.4%p 상승했다.",  // 근거 원문에서 실제로 확인한 문장
        "insufficient_reason": null,     // 근거 부족일 때만 채워짐 (아래 표 참고)
        "insufficient_label": null,
        "evidence": [
          { "title": "...", "url": "https://...", "source": "naver_news",
            "published_at": "2024-03-01", "rating": null,
            "source_type": "news", "source_type_label": "언론 보도",
            "is_primary": false }
        ]
      }
    ],
    "detail": null
  },
  "transcript": {
    "summary": "...", "keywords": ["..."], "tone": "중립",
    "language": "en", "word_count": 24, "coverage_pct": 99.5,
    "signals": { "clickbait": 6, "claim_strength": 0, "tts": 8 }   // 참고용 — 어느 축 점수에도 미반영
  }
}
```

프론트에서 반드시 처리해야 할 것들:

1. **`status`만 표시하고 숫자는 쓰지 마세요.** `face_manipulation`/`whole_video_generation`의 `signals` 안
   점수는 디버그용입니다. 화면에는 `status_label`(또는 `status`를 보고 직접 매핑한 문구)만 씁니다.
2. `status === "unavailable"`이면 "판단 불가"와 사유(`detail`)를 보여주세요. `whole_video_generation`은
   전용 모델이 아직 없어 자가표기가 없는 한 항상 `unavailable`입니다 — 이건 정상 상태입니다.
3. 카드의 `status`(처리 상태)와 `verdict`(검증 판정)를 분리해서 다루세요. `status !== "done"`인 카드는
   `verdict`를 아직 신뢰할 수 없습니다(기본값일 뿐).
4. `verdict: "unverified"`(`근거 부족`)는 **"거짓"이 아니라 "확인 못 함"**입니다. 거짓처럼 보이게
   표시하면 안 됩니다. `insufficient_label`에 왜 판정하지 못했는지가 들어 있으니 함께 보여주세요.
5. **판정 문구는 `verdict_label`을 쓰세요.** 코드값(`supported` 등)을 직접 매핑하지 않아도 됩니다.
   `근거와 일치`는 "이 주장이 참"이 아니라 "우리가 찾은 근거와 일치한다"는 뜻입니다 — 주어가
   주장이 아니라 근거입니다. 화면 문구도 그렇게 읽히게 써주세요.
6. `quote`는 근거 원문에서 **실제로 확인한 문장**입니다. LLM이 인용한 문장이 원문에 없으면 서버가
   판정을 `근거 부족`으로 강등하므로, `quote`가 있는 판정은 원문 대조를 통과한 것입니다.
7. `evidence[].is_primary`가 `true`면 통계 원문·공식 발표 같은 1차 출처입니다. 근거를 나열할 때
   앞에 두거나 표시를 다르게 하면 신뢰도 차이가 전달됩니다.
8. `analysis_status === "partial"`이면 `stages`에서 실패한 단계를 확인해 안내해주세요.

#### 근거 부족 사유 (`insufficient_reason`)

| 코드 | 화면 문구 |
|---|---|
| `no_source` | 관련 자료를 찾지 못함 |
| `not_direct` | 자료는 있으나 주장을 직접 확인하지 못함 |
| `timeout` | 시간 내 확인하지 못함 |
| `time_mismatch` | 주장과 자료의 시점이 다름 |
| `source_conflict` | 출처들이 서로 충돌함 |
| `weak_source` | 출처의 내용이 판정에 충분하지 않음 |
| `partial` | 주장의 일부만 확인됨 |

#### 출처 유형 (`evidence[].source_type`)

`statistics`(통계 원문) · `official`(공식 발표) · `factcheck`(팩트체크 판정) · `news`(언론 보도) ·
`encyclopedia`(백과사전) · `unknown`(기타). 앞의 둘이 1차 출처입니다.

### 에러 응답

어떤 실패든 같은 모양으로 나갑니다. `request_id`는 응답 헤더(`X-Request-ID`)와 서버 로그에
함께 남아 있어, 특정 요청 하나를 로그에서 그대로 찾을 수 있습니다.

```jsonc
{
  "error": {
    "code": "download_failed",     // unsupported_url / invalid_request / job_not_found /
                                   // server_busy / dependency_missing / internal_error
    "message": "영상을 받지 못했습니다: This video is unavailable",
    "retryable": true,             // true면 잠시 후 재시도 안내가 적절합니다
    "stage": "download"
  },
  "request_id": "b0a4df3f1230"
}
```

분석 도중 실패한 job은 `GET /api/jobs/{id}`의 `error`에 같은 구조가 들어갑니다.

## 설정

모든 값은 `DEEPCHECK_` 환경변수로 덮어쓸 수 있습니다 (기본값은 `deepcheck/config.py`).

Docker로 띄울 때는 `docker-compose.yml`의 `environment`에 나열된 것만 컨테이너로 전달됩니다.
그 외 설정을 바꾸려면 레포 루트에 `.env` 파일을 만들어 적으세요 (파일이 없어도 정상 기동하며,
`.gitignore`에 있어 커밋되지 않습니다).

```bash
# .env
DEEPCHECK_BACKLOG=16
DEEPCHECK_MAX_CLAIMS=3
DEEPCHECK_GOOGLE_FACTCHECK_API_KEY=...
```

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `DEEPCHECK_WORKERS` | 3 | 동시 분석 수. 저사양 호스트는 1~2 권장 |
| `DEEPCHECK_LOG_LEVEL` | INFO | 로그 레벨 |
| `DEEPCHECK_LOG_FORMAT` | text | `json`으로 두면 한 줄 JSON 로그 (수집·검색용) |
| `DEEPCHECK_CORS_ORIGINS` | `*` | 허용 오리진. 쉼표 구분 |
| `DEEPCHECK_WHISPER_MODEL_SIZE` | small | STT 모델 크기 |
| `DEEPCHECK_CAPTION_POLICY` | manual | `manual`/`any`/`off` — 자막 사용 정책 |
| `DEEPCHECK_MAX_FRAMES` | 8 | 샘플링 프레임 수 |
| `DEEPCHECK_CLASSIFIER_MODEL` | dima806/... | 딥페이크 분류기 |
| `DEEPCHECK_VLM_MODEL` | (없음) | 지정 시에만 VLM 사용 |
| `DEEPCHECK_VLM_PROVIDER` | ollama | VLM 제공자 |
| `DEEPCHECK_MAX_CLAIMS` | 5 | 영상당 검증할 주장 수. 응답 시간에 직결 |
| `DEEPCHECK_EVIDENCE_PER_CLAIM` | 3 | 주장당 근거 검색 건수 |
| `DEEPCHECK_EVIDENCE_TIMEOUT_SEC` | 8 | 근거 검색 요청 타임아웃 |
| `DEEPCHECK_EVIDENCE_PROVIDERS` | factcheck,wikipedia,wikipedia_en | 근거 검색 수단과 순서 |
| `DEEPCHECK_GOOGLE_FACTCHECK_API_KEY` | (없음) | 있으면 전문 기관 판정 검색 사용 |
| `DEEPCHECK_LEVEL_HIGH` / `_MODERATE` / `_CAUTION` | 70 / 45 / 25 | 등급 경계 |

로그에는 `request_id`와 `job_id`가 함께 붙어(`[req:ab12 job:cd34]`) 동시에 여러 분석이
돌아도 요청 하나를 끝까지 추적할 수 있습니다.

### 근거 검색 수단

`DEEPCHECK_EVIDENCE_PROVIDERS`에 쓴 순서대로 조회합니다 (기본 `factcheck,wikipedia,wikipedia_en`).

| 이름 | 키 필요 | 비고 |
|---|---|---|
| `factcheck` | 필요 | Google Fact Check Tools. **"지지/반박" 판정이 나오려면 이게 있어야 합니다** |
| `wikipedia` / `wikipedia_en` | 불필요 | 한국어·영어 위키백과. 빠릅니다 |
| `gdelt` | 불필요 | **기본 비활성.** 실측 응답이 16초 이상이고 429가 잦아 뺐습니다 |

팩트체크 키가 없으면 관련 근거는 모아 보여주되 모든 주장이 `unverified`로 남습니다.
키는 [Google Cloud 콘솔](https://console.cloud.google.com/)에서 발급받아
`DEEPCHECK_GOOGLE_FACTCHECK_API_KEY`에 넣으면 됩니다.

## 선택 구성 요소

없어도 동작하지만 있으면 정확도가 올라갑니다. 빠졌을 때는 결과의 `detail`과 로그에 이유가 남습니다.

### 얼굴 crop 모델 (권장)

분류기가 얼굴 crop 이미지로 학습된 모델이라, 전체 프레임을 넣으면 정확도가 떨어집니다.
Docker 이미지는 빌드 시 자동으로 받습니다. 로컬 실행은 한 번만:

```bash
mkdir -p deepcheck/models
curl -L -o deepcheck/models/blaze_face_short_range.tflite \
  https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite
```

### VLM 근거 (선택)

Ollama에 비전 모델을 띄우고 `DEEPCHECK_VLM_MODEL` 또는 요청의 `vlm_model`로 지정하면,
"왜 AI로 보이는지"를 자연어 근거로 추가합니다. 지정하지 않으면 실행하지 않습니다.

```bash
ollama pull qwen2.5vl:7b
```

## 배포 시 주의

- torch/torchvision은 **CPU 전용 wheel index**로 설치합니다. 기본 PyPI 휠은 GPU가 없는
  호스트에도 CUDA 런타임(~2GB)을 끌고 옵니다. Dockerfile에 반영돼 있습니다.
- uvicorn은 **단일 워커 프로세스**로 띄웁니다. job 상태를 프로세스 메모리에 보관하므로
  워커가 여러 개면 `GET /api/jobs/{id}`가 무작위로 404가 납니다. 동시성은
  `DEEPCHECK_WORKERS`로 조절하세요.

> yt-dlp 사용은 대상 사이트의 약관과 저작권 정책을 따릅니다.
