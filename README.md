# Conan AI — 백엔드 (be)

영상 URL을 받아 **미디어 조작 가능성**과 **주장 사실성**을 분석하는 백엔드 서비스입니다.

기획·설계 기준은 [docs 레포](https://github.com/Dynamic-Juo/docs)를 따릅니다
(`project/prd.md`, `design/ai-pipeline.md`). 작업 규칙은 [AGENTS.md](AGENTS.md)를 참고하세요.

새 기기에서 이어서 작업할 때는 [현재 상태와 읽는 순서](docs/handoff.md)부터 확인하세요.
[프롬프트·평가 결과](docs/prompt-evaluation.md)와 [M4 맥미니·OrbStack 배포 절차](docs/deployment-mac-mini.md)를 별도로 관리합니다.

2026-09-12 배포 준비: `fix/midpoint-hardening`의 API·Swagger 설명과
[FE 연동 인수인계](docs/frontend-integration.md), [요청·응답·오류 API 계약](docs/api-reference.md)을 보완했습니다.
자막 기본값을 기존 `manual`로 복원한 뒤 로컬 검증은 **329 passed, 2 warnings**입니다. 모의 모델·제공자와
Python 네트워크 차단 fixture를 사용한 결과이며 실제 영상·외부 API·컨테이너 검증은 아닙니다.
백엔드 PR·CI와 기존 개발계 배포를 준비하며, 서버 명령은 각각 승인받고 실행합니다.
문서 저장소의 새 PR과 PR #7 댓글은 이번 작업에서 게시하지 않습니다. 최신 진행 상태는 인수인계를 확인하세요.

이전 2026-09-11 중간 점검: 아래 동작 설명은 작업 복사본의 `fix/midpoint-hardening` 수정안 기준입니다.
배포 서버에는 반영하지 않았고, 이번 검증은 외부 연결과 실제 모델을 사용하지 않는 모의 테스트입니다.
실제 영상 정확도·DeepSeek/NAVER 연동 성공이나 공개 운영 준비 완료를 뜻하지 않습니다.

> **[파이프라인 해부도](docs/pipeline.md)** — 단계별 흐름도, 각 단계의 라이브러리와 실패 처리,
> 모델·라이브러리를 갈아끼울 때 건드릴 곳, 구간별 실측 성능. 검증하거나 무언가를 교체할 때 여기부터 보세요.

## 세 축을 분리한다

제품은 분석 결과를 **하나의 진위 점수로 합치지 않습니다**. 실제 인물이 나온 영상에도
허위 주장이 있을 수 있고, AI로 만든 영상의 발언이 사실일 수도 있기 때문입니다. 미디어
조작도 두 가지를 구분합니다 — 얼굴을 합성·변형한 것과, 영상 전체를 AI가 만든 것은
서로 다른 문제입니다.

| 축 | 응답 필드 | 내용 |
|---|---|---|
| 얼굴 합성·변형 | `face_manipulation` | 얼굴 crop 분류 점수 + 기존 자가표기 신호. 휴리스틱·선택적 VLM은 참고 정보 |
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
얼굴 없는 프레임은 분류와 집계에서 제외합니다. 분류기가 없거나 유효 점수가 없으면 휴리스틱만으로
"정상"을 발표하지 않습니다. 다만 기존 자가표기 키워드가 있으면 별도 신호로 판정에 반영됩니다.
이 키워드 방식은 단순 언급·부정문을 구별하지 못하고 두 미디어 축에 동시에 작용하므로,
정책 변경 승인이 필요한 정확성 한계로 남아 있습니다.

### 판정에 대한 태도

우리가 내는 판정은 **주장의 진위가 아니라 주장과 근거의 관계**입니다. `근거와 일치`는
"이 주장은 참이다"가 아니라 "우리가 찾은 근거는 이 주장과 일치한다"는 뜻입니다.

검색 결과와 전문 기관의 `rating`은 참고 자료이며 그 문자열을 현재 주장 판정으로 바로 옮기지 않습니다.
같은 주장에 관한 판정들이 충돌하면 유보하고, 그 외에는 LLM이 제안한 관계를 서버가 검증합니다.

`근거와 일치`·`근거와 불일치`에는 인용문 일치뿐 아니라 검증된 원문·출처 대응이 필요합니다.
서버는 `content_scope=original`, `provenance_verified=true`, 실제 원문 인용을 확인하고,
1차 출처 하나 또는 서로 다른 검증된 원문 계보 두 개 이상의 충분성 조건을 적용합니다.
이 검증은 `DEEPCHECK_LLM_QUOTE_CHECK=false`로 끌 수 없습니다.

**현재 검색 제공자는 제목·발췌만 수집하며 원문 수집·출처 검증기는 미구현입니다.** 따라서 현재
실제 검색 경로의 자료만으로는 이 조건을 통과할 수 없고 `근거 부족`으로 유보합니다.
모의 원문으로 검증한 코드 경로와 실제 원문 확보 성공을 구분해야 합니다.

- 검색 결과가 없다는 이유만으로 주장을 거짓으로 판정하지 않습니다.
- 근거가 부족하거나 서로 충돌하면 판단을 유보합니다.
- 주장과 근거의 기준 시점이 다르면 수치가 달라도 `근거와 불일치`가 아니라 `근거 부족`입니다.

명시적 규칙 추출(`DEEPCHECK_CLAIM_EXTRACTOR=rule`)은 LLM 없이 실행할 수 있지만, 이 경로도
전문 기관 `rating`만으로 지지·반박을 확정하지 않습니다. `llm` 추출을 요청한 뒤 제공자·호출·응답
검증이 실패하면 규칙 기반으로 몰래 바꾸지 않고 `unavailable`로 표시합니다.

## 파이프라인

실제 실행 순서는 입력 확인·다운로드 → 프레임·얼굴 분석 → 발언 확보 → 주장 추출·검증입니다.
미디어 결과를 먼저 전달하고 주장 카드를 점진적으로 갱신합니다. 두 축이 서로의 판정을 전제하거나
실제 모델이 동시에 실행된다고 가정하지 않습니다.

| 단계 | 모듈 | 도구 |
|---|---|---|
| 오케스트레이션 | `deepcheck/pipeline.py` | 단계별 상태·소요시간 기록 |
| 다운로드·자막 | `deepcheck/downloader.py` | yt-dlp + PyAV (시스템 ffmpeg 불필요) |
| 자막 파싱 | `deepcheck/captions.py` | WebVTT/SRT 직접 파싱 |
| STT | `deepcheck/transcriber.py` | faster-whisper (CTranslate2) |
| 얼굴 crop | `deepcheck/face.py` | MediaPipe |
| 조작 탐지 | `deepcheck/deepfake.py` | 얼굴 crop ViT 분류, 휴리스틱·선택적 VLM 참고 정보 |
| 텍스트 신호 | `deepcheck/analyzer.py` | 자가표기·합성음성·클릭베이트 |
| 주장 검증 | `deepcheck/claims.py`, `deepcheck/prompts.py` | LLM/규칙 추출 + 네이버·위키백과 등 검색 + LLM 판정·인용 검증 |
| 결과 조립 | `deepcheck/report.py` | 두 축 분리, text/JSON/HTML 출력 |
| API | `backend/app.py`, `backend/harness.py` | FastAPI + 워커 풀 |
| 에러·로그 | `deepcheck/errors.py`, `deepcheck/logging_setup.py` | 도메인 예외, request_id 로깅 |

**기본은 수동 CC 우선(`caption_policy: "manual"`)입니다.** 기존 배포 동작을 유지하며,
사용할 CC가 없으면 STT로 전환합니다. 영상에 입힌 글자의 OCR은 아니고 자동 생성 CC는 제외합니다.
등록 CC라는 분류만으로 사람 작성·정확성·발언 전문을 보증하지 않습니다. `off`는 항상 STT,
`any`는 자동 CC도 허용하는 명시적 옵션입니다. 기획 본문과 리뷰의 최종 정리는 별도이며,
이번 보안 수정에 STT 기본값 전환을 섞지 않습니다. [정책 차이와 FE 인수 조건](docs/frontend-integration.md#먼저-구분할-상태)을 확인하세요.

과거 한국어 뉴스 표본에서는 `small` 약 88초, `tiny` 약 53초와 수치·고유명사 오인식이 기록됐습니다.
이는 당시 별도 실행 결과이지 현재 수정안의 성능이나 한국어 전반의 정확도 보증이 아닙니다.
자막도 오기·발언 불일치가 있을 수 있습니다. [당시 실측과 한계](docs/pipeline.md)를 함께 확인하세요.

## 실행

### Docker (독립 로컬 개발 환경)

아래는 새 로컬 개발 환경용 예시입니다. 기존 서비스가 있는 맥미니에서 그대로 실행하지 말고,
서버 명령은 변경 대상·영향을 확인한 뒤 승인받아 [별도 배포 절차](docs/deployment-mac-mini.md)를 따릅니다.

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
python -m deepcheck.cli analyze "URL" --model-size small --json
python -m deepcheck.cli analyze "URL" --no-classifier       # 얼굴 분류 미실행, 휴리스틱만으로 정상 판정하지 않음
python -m deepcheck.cli analyze "URL" --save-transcript     # STT 원문 저장
```

### 테스트

```bash
uv pip install -r requirements-dev.txt
pytest
```

## API

상세 필드·기본값·부분 결과·폴링 종료·오류 처리는 [API 계약](docs/api-reference.md),
Vercel과 Access 연결 순서는 [FE 연동 인수인계](docs/frontend-integration.md)를 기준으로 확인하세요.
`/docs`(Swagger), `/redoc`, `/openapi.json`은 해당 서버 버전의 문서를 제공합니다.
이번 상세 스키마 보완은 미배포이며 도메인의 실제 화면을 재검증하지 않았습니다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/analyze` | `{url, model_size, max_frames, use_classifier, vlm_model, enable_claim_verification, caption_policy, session_id}` → `{job_id, status, session_id, deduplicated}` |
| GET | `/api/jobs/{job_id}` | `{status, progress, message, result, error}` — status 값은 아래 표 참고 |
| GET | `/api/sessions/{session_id}` | 디버그 전용, 기본 비활성(404) |
| GET | `/api/jobs` | 전체 목록 디버그 전용, 기본 비활성(404) |
| GET | `/health` | `{"status":"ok"}` — 프로세스 생존 확인 |
| GET | `/ready` | `{status, harness:{...}}` — 접수 여력 확인 |

분석은 무거우므로 job 방식입니다. POST로 `job_id`를 받고 2~3초 간격으로 폴링하세요.
같은 URL의 작업이 진행 중이면 기존 job을 재사용합니다(`deduplicated: true`).
현재 중복 키는 URL만 보므로 옵션이 달라도 첫 요청의 분석 옵션과 결과를 재사용하는 한계가 있습니다.
`session_id`는 묶음 식별자이지 인증이 아닙니다.
입력은 YouTube URL로 제한하고 메타데이터의 공개 상태·연령 제한·길이를 검사하지만,
YouTube의 실제 Shorts 분류와 한국어 여부를 완전하게 검증하는 기능은 아직 없습니다.

`429`는 두 가지 원인으로 나옵니다. `error.code`로 구분해서 안내해주세요.

| code | 원인 | 안내 |
| --- | --- | --- |
| `session_busy` | 이 세션이 **다른 영상**을 이미 분석 중 (M-07: 세션당 1건) | "이미 분석 중인 영상이 있습니다" |
| `server_busy` | 서버 대기열이 가득 참 | "요청이 많습니다. 잠시 후 다시" |

`GET /api/jobs/{job_id}` 응답에는 결과 외에 화면에 필요한 값이 함께 옵니다.

| 필드 | 용도 |
| --- | --- |
| `display_id` | `CN-A1B2-C3D4` — 화면에 표시하고 피드백 폼에 첨부할 분석 ID |
| `created_at_iso` | 분석 시각 |
| `elapsed_sec` | 경과 시간(초). 완료되면 총 소요 시간으로 고정됩니다 |

### job 상태값

`result`는 **폴링마다 조금씩 채워집니다.** 미디어 조작 두 축은 준비되는 즉시, 주장 카드는
개수가 확정되면 전부(대기 중 상태로) 먼저 나타났다가 하나씩 검증되며 갱신됩니다.
현재 구현은 미디어 처리 후 발언 확보·주장 검증 순서이며, 미디어 실패가 주장 검증을 막지는 않습니다.

| status | 의미 |
|---|---|
| `queued` | 대기열에서 워커를 기다리는 중. `result`는 비어 있다 |
| `processing:collecting` | 영상·프레임을 확보하는 중 |
| `processing:transcribing` | 발언 텍스트(자막 또는 STT)를 확보하는 중 |
| `processing:extracting_claims` | 주장을 추출하고 중복을 합치는 중 |
| `processing:verifying` | 추출한 주장을 검증하는 중. 부분 결과가 있으면 status는 `partially_completed`, stage는 `verifying`일 수 있음 |
| `partially_completed` | 첫 부분 결과가 나왔다. `result`의 키를 보고 뭐가 준비됐는지 확인 |
| `completed` | 모든 분석이 끝났다 |
| `completed_with_limitations` | 일부 분석을 못 했지만 나머지 결과는 있다 (`stages`에서 실패한 단계 확인) |
| `timed_out` | 시간 초과를 인식해 종료한 작업. 완료 결과는 유지하며, 실행 중 다운로드·STT를 정확히 10분에 중단하는 하드 제한은 아직 아님 |
| `failed` | 진행할 수 없는 오류. `error`에 사유 |

### result 스키마 (프론트 계약)

아래 숫자·주장·출처는 형식을 설명하는 예시이지 실제 분석 결과가 아닙니다. 특히 `cited: true`의
원문 조건은 모의 자료로만 검증했으며, 현재 검색 발췌 수집 경로가 이를 확보했다는 뜻이 아닙니다.

```jsonc
{
  "url": "...",
  "media": {
    "title": "...", "uploader": "...",       // 제목 · 채널
    "duration": 123,                          // 길이(초)
    "upload_date": "2026-09-01",              // 게시일 (없으면 null)
    "thumbnail": "https://i.ytimg.com/...",   // 썸네일 URL (없으면 null)
    "video_id": "...", "language": "ko",
    "transcript_source": "stt",               // stt | caption — 발언 위치 옆에 표시할 출처
    "stt_coverage_pct": 99.8,                 // 입력 오디오 길이 비율, 인식 정확도 아님
    "transcript_coverage_basis": "input_audio_duration",
    "transcript_segment_coverage_pct": 21.0,  // 텍스트 타임스탬프 구간 합집합 비율
    "transcript_coverage_detail": "입력 오디오 길이 비율이며 발언 완전성·인식 정확도를 뜻하지 않는다."
  },
  "analysis_status": "partial",         // 필수 축 누락·단계 실패·미완료 주장도 partial. job.status가 최종 기준
  "stages": {                            // 무엇을 했고 무엇을 못 했는지
    "download":  { "status": "ok", "detail": null, "elapsed_sec": 6.1 },
    "frames":    { "status": "ok", "detail": "8장 추출", "elapsed_sec": 1.2 },
    "transcript":{ "status": "ok", "detail": "STT 226단어, 텍스트 구간 비율 21.0% (정확도는 별도 검증)", "elapsed_sec": 58.4 },
    "media_manipulation":  { "status": "ok", "detail": null, "elapsed_sec": 10.0 },
    "claim_verification":  { "status": "ok", "detail": "주장 8건 (...)", "elapsed_sec": 14.2 },
    "cleanup": { "status": "ok", "detail": "요청별 임시 파일을 정리했다." }
  },
  "face_manipulation": {
    "status": "suspected",               // suspected / no_clear_signs / inconclusive / unavailable
    "status_label": "조작 의심",          // 화면에 그대로 쓸 수 있는 한국어 문구
    "detail": "프레임 8장을 분석해 그중 4장에서 얼굴을 찾았다. ...",  // 분석 범위. 한계가 있으면 뒤에 덧붙는다
    "evidence": ["frame_000.jpg: fake 98.4% (라벨 Fake) [얼굴 crop 적용]"],
    "signals": { "combined_risk": 81.0, "frames_analyzed": 8, "frames_with_face": 4, "..." : "..." }
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
    "status": "analyzed",                // analyzed | no_claims | unavailable
    "summary": {
      "total": 8,
      // 처리 상태별 — 화면 요약의 "완료 8 · 미완료 0 · 시간 초과 0"
      "pending": 0, "verifying": 0, "done": 8, "failed": 0, "timed_out": 0,
      // 판정별 — 화면 요약의 "일치 2 · 불일치 3 · 근거 부족 3"
      "supported": 2, "refuted": 3, "unverified": 3
    },
    "claims": [
      {
        "text": "2024년 실업률이 3.2% 감소했다",
        "start": 12.5, "end": 18.0,      // 발언 위치 (못 찾으면 null)
        "time_precision": "exact",       // exact | approx | null — approx면 "약 00:28"처럼 표시할 것
        "mentions": [],                  // 같은 주장이 반복될 때의 문맥·횟수
        "status": "done",                // pending / verifying / done / failed / timed_out (카드 처리 상태)
        "verdict": "refuted",            // supported / refuted / unverified (status가 done일 때만 의미 있음)
        "verdict_label": "근거와 불일치",  // 화면에 그대로 쓰면 되는 문구
        "reason": "통계청 자료는 같은 기간 실업률이 올랐다고 밝히고 있다.",  // 카드의 "판정 근거"
        "quote": "2024년 연간 실업률은 전년 대비 0.4%p 상승했다.",  // 대표 인용 (근거별 인용은 아래)
        "insufficient_reason": null,     // 근거 부족일 때만 채워짐 (아래 표 참고)
        "insufficient_label": null,
        "evidence": [
          {
            "title": "...", "url": "https://...",
            "source": "naver_news",            // 어느 검색 수단에서 왔는지 (내부 식별용)
            "publisher": "kostat.go.kr",       // 발행처 — 화면의 "발행 기관 · 언론사"
            "published_at": "2024-03-01",
            "source_type": "official",
            "source_type_label": "공식 발표",   // 화면의 출처 유형 배지
            "is_primary": true,                // 1차 출처(통계 원문·공식 발표)면 true
            "content_scope": "original",       // 검증된 원문을 확보한 경우에만 가능
            "provenance_verified": true,        // 서버 원문 검증 경로의 값, LLM이 부여할 수 없음
            "rating": null,                    // 전문 기관 판정 표기가 있을 때만
            "cited": true,                     // 판정에 실제로 쓰였는지. false면 "참고 자료"
            "cite_reason": "이 자료가 주장을 반박하는 이유.",  // 근거 카드에 그대로 표시
            "quote": "2024년 연간 실업률은 전년 대비 0.4%p 상승했다."  // 원문 대조를 통과한 발췌
          }
        ]
      }
    ],
    "detail": null
  },
  "transcript": {
    "summary": "...", "keywords": ["..."], "tone": "중립",
    "language": "ko", "word_count": 226, "coverage_pct": 99.8,
    "coverage_basis": "input_audio_duration", "segment_coverage_pct": 21.0,
    "coverage_detail": "입력 오디오 길이 비율이며 발언 완전성·인식 정확도를 뜻하지 않는다.",
    "signals": { "clickbait": 6, "claim_strength": 0, "tts": 8 }   // 참고용 — 어느 축 점수에도 미반영
  }
}
```

프론트에서 반드시 처리해야 할 것들:

1. **`status`만 표시하고 숫자는 쓰지 마세요.** `face_manipulation`/`whole_video_generation`의 `signals` 안
   점수는 디버그용입니다. 화면에는 `status_label`(또는 `status`를 보고 직접 매핑한 문구)만 씁니다.
2. `status === "unavailable"`이면 "판단 불가"와 사유(`detail`)를 보여주세요. `whole_video_generation`은
   전용 모델이 아직 없어 자가표기가 없는 한 `unavailable`입니다. 필수 분석 축을 못 한 한계이므로
   전체 결과도 `partial`로 표시합니다.
3. 카드의 `status`(처리 상태)와 `verdict`(검증 판정)를 분리해서 다루세요. `status !== "done"`인 카드는
   `verdict`를 아직 신뢰할 수 없습니다(기본값일 뿐).
4. `verdict: "unverified"`(`근거 부족`)는 **"거짓"이 아니라 "확인 못 함"**입니다. 거짓처럼 보이게
   표시하면 안 됩니다. `insufficient_label`에 왜 판정하지 못했는지가 들어 있으니 함께 보여주세요.
5. **판정 문구는 `verdict_label`을 쓰세요.** 코드값(`supported` 등)을 직접 매핑하지 않아도 됩니다.
   `근거와 일치`는 "이 주장이 참"이 아니라 "우리가 찾은 근거와 일치한다"는 뜻입니다 — 주어가
   주장이 아니라 근거입니다. 화면 문구도 그렇게 읽히게 써주세요.
6. **`evidence[].cited`가 `false`인 자료는 "참고 자료"이지 판정 근거가 아닙니다.** 근거 부족 카드에
   붙은 자료가 여기 해당합니다. 판정 근거와 같은 자리에 섞어 보여주면, 판정하지 않은 것을 판정한
   것처럼 보여주게 됩니다.
7. `cited: true`는 서버의 원문·충분성·인용 검증을 통과한 판정 근거입니다. 검색 발췌 인용만으로는
   이 값을 확정하지 않습니다. `cite_reason`은 LLM의 설명이며 문자열 검증만으로 의미 정확성을 보장하지 않습니다.
8. `evidence[].is_primary`는 출처 유형 분류 정보입니다. 이것만으로 출처 진위·원문 확보를 보장하지 않으며,
   `content_scope`·`provenance_verified`와 함께 해석해야 합니다.
9. **`time_precision`이 `"approx"`면 정확한 위치가 아닙니다.** 핵심어 겹침으로 추정한 값이라 몇 초
   어긋날 수 있습니다. `"약 00:28"`처럼 근삿값임이 드러나게 표시해주세요. `null`이면 위치를 찾지
   못한 것이라 표시를 빼면 됩니다.
10. `analysis_status === "partial"`이면 `stages`뿐 아니라 각 축의 `unavailable`과 실패·미완료 카드도 안내해주세요.
11. `coverage_pct`·`stt_coverage_pct`의 100%는 "발언을 모두 정확히 인식했다"는 뜻이 아닙니다.
    `coverage_basis`, `segment_coverage_pct`, `coverage_detail`을 함께 확인하세요. 구간 비율도 정확도 점수는 아닙니다.

#### 화면별로 어디를 보면 되는지

조정준 팀장이 공유한 시안(S-01 ~ S-04D) 기준입니다.

| 화면 | 표시할 것 | 어디서 |
| --- | --- | --- |
| S-02 상단 | 썸네일 · 채널 · 길이 · 게시일 | `result.media.thumbnail` / `uploader` / `duration` / `upload_date` |
| S-02 진행 | 단계 문구 | `job.message`, 단계 구분은 `job.stage` (부분 결과 이후에도 갱신) |
| S-02 하단 | 분석 ID · 시각 | `job.display_id`(`CN-A1B2-C3D4`) / `job.created_at_iso` |
| S-02·S-03 경과 | "경과 01:52" | `job.processing_elapsed_sec`, 대기 시간은 `job.queue_wait_sec` |
| S-03 ① | "검증할 주장 8개를 찾았습니다" | `claim_verification.summary.total` |
| S-03 ② | "3/8개 완료" | `summary.done` / `summary.total` |
| S-03 ⑦ | "00:12 ~ 00:19 · 음성 인식" | `claim.start`·`end`·`time_precision`, 출처는 `media.transcript_source` |
| S-03 접힘 | 자료 개수·출처 유형. "원문 포함"은 확보·검증했을 때만 | `claim.evidence.length`, `is_primary`와 `content_scope`·`provenance_verified`를 함께 확인 |
| S-04 요약 | "일치 2 · 불일치 3 · 근거 부족 3" | `summary.supported` / `refuted` / `unverified` |
| S-04 요약 | "완료 8 · 미완료 0 · 시간 초과 0" | `summary.done` / `pending`+`verifying`+`failed` / `timed_out` |
| S-04 미디어 축 | 판단 근거와 분석 범위 | `face_manipulation.status_label` + `detail` |
| S-04D 근거 카드 | 출처 유형 배지 · 발행일 · 발행 기관 | `evidence[].source_type_label` / `published_at` / `publisher` |
| S-04D 근거 카드 | "이 자료가 주장을 반박하는 이유" | `evidence[].cite_reason` |
| S-04 근거 부족 | "판정하지 못한 이유" | `claim.insufficient_label` |
| S-04 근거 부족 | "참고 자료 2건 (판정에는 사용하지 않음)" | `evidence` 중 `cited: false`인 것 |

**아직 서버가 주지 않는 것**

- `S-03 ④ "분석이 예상보다 오래 걸리고 있습니다"` — `job.processing_elapsed_sec`로 프런트에서 판단해주세요.
  서버가 별도 신호를 주지 않습니다.
- 출처 유형 라벨이 시안과 조금 다릅니다. 시안의 `정부·공공기관`은 서버에서 `공식 발표`(`official`)로
  나갑니다. 화면 문구를 바꾸실지, 서버 라벨을 맞출지 정해주시면 맞추겠습니다.

#### `claim_verification.status`

| 값 | 의미 | 화면 |
| --- | --- | --- |
| `analyzed` | 주장을 뽑아 검증했다 | 카드 목록 |
| `no_claims` | 분석은 정상이었지만 **검증할 주장이 없었다** | 카드 목록 대신 안내 문구(`detail`) |
| `unavailable` | 발언 확보·주장 추출 실패 또는 옵션 해제로 **검증 자체를 못 했다** | 분석 불가 사유 안내 |

`no_claims`와 `unavailable`을 같게 다루면 안 됩니다. 앞은 "볼 게 없었다"이고 뒤는 "보지 못했다"입니다.

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
`encyclopedia`(백과사전) · `unknown`(기타). 앞의 둘은 1차 출처로 분류하지만, 유형 분류 자체가 원문 확보·검증을 뜻하지는 않습니다.

### 에러 응답

아래는 문서화한 API 요청 오류의 기본 형태입니다. 비동기 분석 실패는 조회 HTTP 200 안의
`job.error`·`result.stages`로 전달될 수 있습니다. Cloudflare 로그인 HTML·리다이렉트와
FastAPI 기본 404·405는 이 봉투와 다를 수 있습니다. `request_id`는 응답 헤더(`X-Request-ID`)와
서버 로그에서 해당 API 요청을 찾는 데 사용합니다. [오류 계약](docs/api-reference.md)을 함께 확인하세요.

```jsonc
{
  "error": {
    "code": "download_failed",     // unsupported_url / invalid_request / job_not_found /
                                   // server_busy / dependency_missing / internal_error
    "message": "영상을 받지 못했습니다. 공개 상태와 지원 조건을 확인해주세요.",
    "retryable": true,             // true면 잠시 후 재시도 안내가 적절합니다
    "stage": "download"
  },
  "request_id": "b0a4df3f1230"
}
```

분석 도중 실패한 job은 `GET /api/jobs/{id}`의 `error`에 같은 구조가 들어갑니다.

## 설정

모든 값은 `DEEPCHECK_` 환경변수로 덮어쓸 수 있습니다 (기본값은 `deepcheck/config.py`).

독립 로컬 개발용 `docker-compose.yml`은 선택적 `.env`를 `env_file`로 읽으며 `environment`에
명시된 항목이 우선합니다. 맥미니용 `compose.home.yml`은 `.env.home` 또는 명시한 `CONAN_ENV_FILE`을
읽습니다. 두 파일은 자동 동기화되지 않습니다. 실제 배포 설정 변경은 별도 승인과 절차를 따릅니다.

```bash
# .env
DEEPCHECK_BACKLOG=16
DEEPCHECK_MAX_CLAIMS=0
DEEPCHECK_GOOGLE_FACTCHECK_API_KEY=...
```

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `DEEPCHECK_WORKERS` | 1 | 동시 분석 영상 수. MVP 기본값 |
| `DEEPCHECK_CLAIM_WORKERS` | 3 | 영상 한 건 안의 동시 주장 검증 수 |
| `DEEPCHECK_BACKLOG` | 64 | 실제 대기열 상한. 실행 워커와 별도로 제한 |
| `DEEPCHECK_ENABLE_DEBUG_ENDPOINTS` | false | 전체 job·session 목록. 공개 기본값은 비활성 |
| `DEEPCHECK_LOG_LEVEL` | INFO | 로그 레벨 |
| `DEEPCHECK_LOG_FORMAT` | text | `json`으로 두면 한 줄 JSON 로그 (수집·검색용) |
| `DEEPCHECK_CORS_ORIGINS` | `*` | 허용 오리진. 쉼표 구분 |
| `DEEPCHECK_WHISPER_MODEL_SIZE` | small | STT 모델 크기 |
| `DEEPCHECK_CAPTION_POLICY` | manual | 업로더 등록 CC 우선, 없으면 STT. `off`는 항상 STT, `any`는 자동 CC도 허용 |
| `DEEPCHECK_MAX_FRAMES` | 8 | 샘플링 프레임 수 |
| `DEEPCHECK_CLASSIFIER_MODEL` | dima806/... | 딥페이크 분류기 |
| `DEEPCHECK_VLM_MODEL` | (없음) | 지정 시에만 VLM 사용 |
| `DEEPCHECK_VLM_PROVIDER` | ollama | VLM 제공자 |
| `DEEPCHECK_MAX_CLAIMS` | 0 | 0은 개수 상한 없음. 일부만 검증하도록 바꾸는 것은 제품 범위에 영향 |
| `DEEPCHECK_LLM_PROVIDER` | off | DeepSeek 등 제공자는 별도 설정 필요 |
| `DEEPCHECK_CLAIM_EXTRACTOR` | rule | `rule`/`llm`. LLM 모드 실패는 분석 불가로 표시 |
| `DEEPCHECK_FRAME_AGGREGATION` | trimmed_mean | 얼굴 분류에 성공한 점수만 절사평균. 임계값 재검증은 별도 |
| `DEEPCHECK_EVIDENCE_PER_CLAIM` | 3 | 주장당 근거 검색 건수 |
| `DEEPCHECK_EVIDENCE_TIMEOUT_SEC` | 8 | 근거 검색 요청 타임아웃 |
| `DEEPCHECK_EVIDENCE_PROVIDERS` | factcheck,naver_news,naver_encyc,wikipedia,wikipedia_en | 자격 정보 없는 수단은 제외 |
| `DEEPCHECK_GOOGLE_FACTCHECK_API_KEY` | (없음) | 있으면 전문 기관 판정 검색 사용 |
| `DEEPCHECK_LEVEL_HIGH` / `_MODERATE` / `_CAUTION` | 70 / 45 / 25 | 등급 경계 |

로그에는 `request_id`와 `job_id`가 함께 붙어(`[req:ab12 job:cd34]`) 동시에 여러 분석이
돌아도 요청 하나를 끝까지 추적할 수 있습니다.

### 근거 검색 수단

`DEEPCHECK_EVIDENCE_PROVIDERS`에 쓴 순서대로 조회합니다
(기본 `factcheck,naver_news,naver_encyc,wikipedia,wikipedia_en`).

| 이름 | 키 필요 | 비고 |
|---|---|---|
| `factcheck` | 필요 | Google Fact Check Tools. 판정 문자열도 참고 자료이며 직접 지지·반박으로 옮기지 않음 |
| `naver_news` / `naver_encyc` | 필요 | NAVER 뉴스·백과 검색. API 키는 백엔드 환경에만 보관 |
| `wikipedia` / `wikipedia_en` | 불필요 | 한국어·영어 위키백과 검색 발췌 |
| `gdelt` | 불필요 | 기본 비활성. 과거 실측에서 16초 이상·429가 기록됨; 현재 재검증 아님 |

팩트체크 키 유무만으로 판정 가능 여부가 정해지지 않습니다. 검증된 원문을 확보하지 못하면
어떤 검색 수단이든 자료는 참고로 표시하고 유보합니다. 제공자가 정상 조회했지만 결과가 없는 경우와
조회 자체가 실패한 경우도 구분하며, 후자는 성공적인 `no_source`로 표시하지 않습니다.

## 선택 구성 요소

구성 요소가 빠져도 가능한 나머지 분석은 계속합니다. 추가했다고 정확도가 자동 보장되지는 않으며,
실패·누락 이유는 결과의 `detail`과 로그에 남습니다.

### 얼굴 crop 모델 (얼굴 기반 분석에 필요)

현재 분류기는 얼굴 crop 입력을 사용합니다. 모델이 없거나 얼굴을 못 찾으면 전체 프레임으로 대체하지 않습니다.
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
