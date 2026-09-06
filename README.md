# Conan AI — 백엔드 (be)

영상 URL을 받아 **미디어 조작 가능성**과 **주장 사실성**을 분석하는 백엔드 서비스입니다.

기획·설계 기준은 [docs 레포](https://github.com/Dynamic-Juo/docs)를 따릅니다
(`project/prd.md`, `design/ai-pipeline.md`). 작업 규칙은 [AGENTS.md](AGENTS.md)를 참고하세요.

## 두 축을 분리한다

제품은 두 분석 결과를 **하나의 진위 점수로 합치지 않습니다**. 실제 인물이 나온 영상에도
허위 주장이 있을 수 있고, AI로 만든 영상의 발언이 사실일 수도 있기 때문입니다.

| 축 | 응답 필드 | 현재 상태 |
|---|---|---|
| 미디어 조작 탐지 | `media_manipulation` | 구현됨 (프레임 분류기 + 휴리스틱 + 자가표기 + 선택적 VLM) |
| 주장 사실성 검증 | `claim_verification` | **미구현** — 주장 선정·근거 검색·판정 기준이 PRD에서 미정(팀 결정 대기) |

또 하나의 원칙은 **"분석 못 함"과 "분석했더니 정상"을 구분**하는 것입니다. 프레임을 한 장도
못 뽑았을 때 위험도 0점을 주면 실패가 무죄 판정으로 둔갑합니다. 그런 경우 `risk`는 `null`,
`status`는 `unavailable`, `level`은 `판단 불가`입니다.

## 파이프라인

```
URL ─► 다운로드(yt-dlp) ─┬─► 프레임 추출 ─► 얼굴 crop ─► 딥페이크 분류 ─┐
                        │                                          ├─► 결과(두 축)
                        └─► STT(faster-whisper) ─► 텍스트 신호 분석 ─┘
```

| 단계 | 모듈 | 도구 |
|---|---|---|
| 오케스트레이션 | `deepcheck/pipeline.py` | 단계별 상태·소요시간 기록 |
| 다운로드 | `deepcheck/downloader.py` | yt-dlp + PyAV (시스템 ffmpeg 불필요) |
| STT | `deepcheck/transcriber.py` | faster-whisper (CTranslate2) |
| 얼굴 crop | `deepcheck/face.py` | MediaPipe |
| 조작 탐지 | `deepcheck/deepfake.py` | ViT 분류기 + 휴리스틱 + 선택적 VLM |
| 텍스트 신호 | `deepcheck/analyzer.py` | 자가표기·합성음성·클릭베이트 |
| 결과 조립 | `deepcheck/report.py` | 두 축 분리, text/JSON/HTML 출력 |
| API | `backend/app.py`, `backend/harness.py` | FastAPI + 워커 풀 |

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
    "status": "not_implemented",
    "claims": [], "evidence": [],
    "detail": "주장 사실성 검증은 아직 구현되지 않았다(팀 결정 대기)."
  },
  "transcript": {
    "summary": "...", "keywords": ["..."], "tone": "중립",
    "language": "en", "word_count": 24, "coverage_pct": 99.5,
    "signals": { "clickbait": 6, "claim_strength": 0 }   // 참고용 — 조작 점수에 미반영
  }
}
```

프론트에서 반드시 처리해야 할 두 가지:

1. `media_manipulation.status === "unavailable"`이면 점수 대신 "판단 불가"와 사유(`detail`)를 보여주세요.
2. `analysis_status === "partial"`이면 `stages`에서 실패한 단계를 확인해 안내해주세요.

## 설정

모든 값은 `DEEPCHECK_` 환경변수로 덮어쓸 수 있습니다 (기본값은 `deepcheck/config.py`).

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `DEEPCHECK_WORKERS` | 3 | 동시 분석 수. 저사양 호스트는 1~2 권장 |
| `DEEPCHECK_LOG_LEVEL` | INFO | 로그 레벨 |
| `DEEPCHECK_WHISPER_MODEL_SIZE` | small | STT 모델 크기 |
| `DEEPCHECK_MAX_FRAMES` | 8 | 샘플링 프레임 수 |
| `DEEPCHECK_CLASSIFIER_MODEL` | dima806/... | 딥페이크 분류기 |
| `DEEPCHECK_VLM_MODEL` | (없음) | 지정 시에만 Ollama VLM 사용 |
| `DEEPCHECK_LEVEL_HIGH` / `_MODERATE` / `_CAUTION` | 70 / 45 / 25 | 등급 경계 |

로그에는 job_id가 붙어 있어(`[job:abc123]`) 동시에 여러 분석이 돌아도 구분됩니다.

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
