# Conan AI — 백엔드 (be)

영상 URL을 받아 **미디어 조작 가능성**과 **주장 사실성**을 분석하는 백엔드 서비스입니다.

기획·설계 기준은 [docs 레포](https://github.com/Dynamic-Juo/docs)를 따릅니다
(`project/prd.md`, `design/ai-pipeline.md`). 작업 규칙은 [AGENTS.md](AGENTS.md)를 참고하세요.

> **[파이프라인 해부도](docs/pipeline.md)** — 단계별 흐름도, 각 단계의 라이브러리와 실패 처리,
> 모델·라이브러리를 갈아끼울 때 건드릴 곳, 구간별 실측 성능. 검증하거나 무언가를 교체할 때 여기부터 보세요.

## 두 축을 분리한다

제품은 두 분석 결과를 **하나의 진위 점수로 합치지 않습니다**. 실제 인물이 나온 영상에도
허위 주장이 있을 수 있고, AI로 만든 영상의 발언이 사실일 수도 있기 때문입니다.

| 축 | 응답 필드 | 내용 |
|---|---|---|
| 미디어 조작 탐지 | `media_manipulation` | 프레임 분류기 + 휴리스틱 + 자가표기 + 선택적 VLM |
| 주장 사실성 검증 | `claim_verification` | 주장 추출 → 외부 근거 검색 → 지지·반박·판단 유보 |

또 하나의 원칙은 **"분석 못 함"과 "분석했더니 정상"을 구분**하는 것입니다. 프레임을 한 장도
못 뽑았을 때 위험도 0점을 주면 실패가 무죄 판정으로 둔갑합니다. 그런 경우 `risk`는 `null`,
`status`는 `unavailable`, `level`은 `판단 불가`입니다.

이 원칙은 얼굴 검출에도 적용됩니다. 지금 쓰는 분류기는 얼굴 crop 이미지로 학습된 모델이라,
영상에서 얼굴을 한 명도 못 찾으면 전체 프레임에 대한 점수를 근거로 쓸 수 없습니다. 이때도
낮은 점수를 "정상"으로 발표하지 않고 판단을 유보합니다.

### 판정에 대한 태도

주장 검증에서 우리가 **직접 지지·반박을 선언하는 경우는 전문 기관이 이미 검증해 공개한
판정을 찾았을 때뿐**입니다. 나머지는 관련 근거를 모아 보여주되 판정은 유보합니다.
키워드가 겹친다는 이유로 참·거짓을 단정하면 그럴듯한 오답을 만들어낼 뿐입니다.

- 검색 결과가 없다는 이유만으로 주장을 거짓으로 판정하지 않습니다.
- 근거가 부족하거나 서로 충돌하면 판단을 유보합니다.

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
| GET | `/api/jobs/{job_id}` | `{status, progress, message, result, error}` — status: queued/running/done/error |
| GET | `/api/sessions/{session_id}` | 해당 세션이 요청한 job 목록 |
| GET | `/health` | `{status, harness:{...}}` |

분석은 무거우므로 job 방식입니다. POST로 `job_id`를 받고 2초 간격 정도로 폴링하세요.
같은 URL이 이미 처리 중이면 새 job을 만들지 않고 기존 job을 재사용합니다(`deduplicated: true`).
대기열이 가득 차면 `429`를 반환하니 잠시 후 재시도하면 됩니다.

### result 스키마 (프론트 계약)

```jsonc
{
  "url": "...",
  "media": { "title": "...", "uploader": "...", "duration": 26, "video_id": "..." },
  "analysis_status": "complete",        // 한 단계라도 실패/건너뜀이면 "partial"
  "stages": {                            // 무엇을 했고 무엇을 못 했는지
    "download":  { "status": "ok", "detail": null, "elapsed_sec": 6.1 },
    "frames":    { "status": "ok", "detail": "8장 추출", "elapsed_sec": 1.2 },
    "transcript":{ "status": "ok", "detail": "24단어, 커버리지 99.5%", "elapsed_sec": 12.0 }
  },
  "media_manipulation": {
    "status": "analyzed",                // 또는 "unavailable"
    "risk": 63.0,                        // unavailable이면 null
    "level": "상당함",                    // 매우 높음 / 상당함 / 주의 필요 / 낮음 / 판단 불가
    "frames_analyzed": 8,
    "frames_fake": 1,
    "method": "heuristics+ViT-classifier",
    "detail": null,                      // 강등된 경우 그 사유
    "signals": {
      "visual":          { "available": true, "risk": 46.9, "classifier_used": true },
      "self_disclosure": { "risk": 70, "evidence": ["제목/설명에 자가표기 발견: \"ai generated\""] },
      "tts":             { "risk": 8 },
      "vlm":             { "summary": null }
    },
    "evidence": ["frame_000.jpg: fake 98.4% (라벨 Fake)", "..."]
  },
  "claim_verification": {
    "status": "analyzed",                // 또는 "unavailable"
    "summary": { "total": 3, "supported": 0, "refuted": 1, "unverified": 2 },
    "claims": [
      {
        "text": "2024년 실업률이 3.2% 감소했다",
        "start": 12.5, "end": 18.0,      // 영상에서 이 말이 나온 위치
        "verdict": "refuted",            // supported / refuted / unverified
        "reason": "전문 기관 판정: \"False\"",
        "evidence": [
          { "title": "...", "url": "https://...", "source": "factcheck",
            "published_at": "2024-03-01", "rating": "False" }
        ]
      }
    ],
    "detail": null
  },
  "transcript": {
    "summary": "...", "keywords": ["..."], "tone": "중립",
    "language": "en", "word_count": 24, "coverage_pct": 99.5,
    "signals": { "clickbait": 6, "claim_strength": 0 }   // 참고용 — 조작 점수에 미반영
  }
}
```

프론트에서 반드시 처리해야 할 세 가지:

1. `media_manipulation.status === "unavailable"`이면 점수 대신 "판단 불가"와 사유(`detail`)를 보여주세요.
2. `analysis_status === "partial"`이면 `stages`에서 실패한 단계를 확인해 안내해주세요.
3. `verdict: "unverified"`는 **"거짓"이 아니라 "확인 못 함"**입니다. 거짓처럼 보이게 표시하면 안 됩니다.
   `reason`에 왜 유보했는지가 들어 있으니 함께 보여주세요.

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
