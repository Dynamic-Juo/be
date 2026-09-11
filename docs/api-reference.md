# 프론트용 API 계약

기준일: 2026-09-12. `fix/midpoint-hardening`의 배포 준비용 코드 계약이다. 기존 배포 기록의 main `472aff7`과 다르며 이 문서 작성 시점에는 미배포다. PR·CI·실제 서버 반영 상태는 [인수인계](handoff.md)를 따른다. 이번에 실제 영상·외부 제공자를 재검증하지 않았다.

연결·인증·Vercel 담당 업무는 [프론트 연동 인수인계](frontend-integration.md)를 먼저 읽는다. 변경 배포 후에는 해당 서버 `/openapi.json`과 적용 커밋을 함께 확인한다. 아래 JSON은 형식 설명용 가상 예시이며 실제 분석 결과나 실행할 테스트 영상이 아니다.

## 주소와 엔드포인트

개발 기본 주소 기록은 `https://conan-api-dev.dotseven.cloud`다. 예상 문서 경로는 `/docs`(Swagger), `/openapi.json`(OpenAPI), `/redoc`(읽기용)이며 이번에 외부 응답을 재검증하지 않았다. URL에 내부 Docker 포트 `:8000`을 붙이지 않는다.

루트 `/`는 등록하지 않아 로그인 후에도 `{"detail":"Not Found"}`가 나올 수 있다. 접속 확인은 `/health`, 문서 확인은 `/docs`를 사용한다. 2026-09-12 미인증 `/health`의 Access 로그인 이동은 브라우저에서 확인했지만 로그인 후 health·Swagger와 영상 분석 성공을 검증한 것은 아니다.

| 메서드 | 경로 | 성공 HTTP | 용도 |
| --- | --- | --- | --- |
| POST | `/api/analyze` | `200` | 분석 접수. 완료 결과가 아니라 job 식별자 반환 |
| GET | `/api/jobs/{job_id}` | `200` | 한 작업의 전체 스냅샷·부분 결과·오류 조회 |
| GET | `/health` | `200` | `{"status":"ok"}`. 모델·키·분석 성공 보증 아님 |
| GET | `/ready` | `200` | status=ready 또는 saturated와 내부 접수 통계 |
| GET | `/api/jobs` | 기본 `404` | 디버그 전용 전체 목록. FE 사용 금지 |
| GET | `/api/sessions/{session_id}` | 기본 `404` | 디버그 전용 세션 목록. FE 사용 금지 |

접수는 `202`가 아니라 현재 구현상 `200`이다. `/ready`도 포화 시 HTTP 503으로 바뀌지 않고 본문의 `status=saturated`를 반환한다. 준비 중 harness를 사용할 수 없는 경로는 현재 `429/server_busy`다. 디버그 목록은 `DEEPCHECK_ENABLE_DEBUG_ENDPOINTS=false`에서 닫고 공개 OpenAPI에도 포함하지 않는다.

Swagger에는 `getHealth`, `getReadiness`, `submitAnalysis`, `getAnalysisJob` 네 operation과 중첩 Schemas를 제공한다. `/docs`는 credentials를 포함하도록 구성하지만, 이 설정이 Vercel의 CORS·Access 인증을 대신하지는 않는다. 읽기만 하다 분석 요청을 보내지 않도록 Try it out은 사용자가 직접 선택해야 한다. Execute를 누르면 실제 서버 작업이 발생할 수 있다.

## 요청 접수

`POST /api/analyze`, `Content-Type: application/json`.

```json
{
  "url": "https://www.youtube.com/shorts/AbCdEfGhI_1",
  "session_id": "fe-session-example"
}
```

예시 영상 ID는 가상 값이다. 실제 시험은 승인한 지원 영상으로 한다. MVP 화면은 합의한 서버 기본값을 사용하며, 이 문서가 일반 사용자용 모델·자막 선택 UI를 새로 결정한 것은 아니다.

| 필드 | 형식·검증 | 생략 시 코드 기본값 | 주의 |
| --- | --- | --- | --- |
| `url` | 필수 string, 1~2048자 | 없음 | YouTube URL만 허용 |
| `session_id` | string 또는 null, 최대 64자 | 새 ID 생성 | 같은 브라우저의 다음 요청에 응답 값을 재사용. 인증 토큰 아님 |
| `model_size` | tiny, base, small, medium, large-v3 | `small` | 서버 `DEEPCHECK_WHISPER_MODEL_SIZE`에 따라 달라짐 |
| `max_frames` | integer, 0~64 | `8` | `DEEPCHECK_MAX_FRAMES`에 따라 달라짐. 0은 미디어 분석을 못 할 수 있음 |
| `use_classifier` | boolean | `true` | false를 정상 영상 판정으로 읽지 않음 |
| `vlm_model` | string 또는 null, 최대 128자 | 미설정 시 null | 서버 `DEEPCHECK_VLM_MODEL`에 따라 달라짐. FE 임의 모델 선택 비권장 |
| `enable_claim_verification` | boolean | `true` | false는 주장 축 미실행이며 성공의 대용이 아님 |
| `caption_policy` | off, manual, any | `manual` | `DEEPCHECK_CAPTION_POLICY`에 따라 달라짐 |

표는 환경변수를 주지 않은 **코드 기본값**이다. 보안 수정에 정책 전환을 섞지 않도록 기존 `manual`로 복원했다. `manual`은 등록 CC 우선이며 자동 생성 CC를 제외하고 사용할 CC가 없으면 STT로 전환한다. CC의 정확성·전문을 보증하지 않으며 [기획 본문과 리뷰의 정리](frontend-integration.md#먼저-구분할-상태)는 별도다. 배포 환경이 명시한 값은 유지하므로 인수 전에 적용 정책·버전을 확인한다. `workdir`, `keep_workdir`, `save_transcript`, API 키, LLM 제공자·모델은 공개 요청 필드가 아니다.

LLM 제공자와 주장 추출기는 BE 환경 설정이다. 코드 기본은 `DEEPCHECK_LLM_PROVIDER=off`, `DEEPCHECK_CLAIM_EXTRACTOR=rule`이고, 배포 예시의 DeepSeek+llm 구성과 구분한다. [기존 PR의 LLM 사용 설명](https://github.com/Dynamic-Juo/docs/pull/7#discussion_r3989716198)은 당시 공유 기록이지 이번 live 환경 재검증이 아니다. FE 요청의 `model_size`는 Whisper 크기이며 DeepSeek 모델 설정이 아니다. 키를 프론트 번들·공개 환경변수로 전달하지 않는다.

### 지원 경계

URL 파서는 youtube.com, www.youtube.com, m.youtube.com의 `/watch?v=…`, `/shorts/…`, youtu.be의 `/…`에서 11자 영상 ID를 추출해 HTTPS watch URL로 정규화한다. 임의 웹 URL·내부 IP·비기본 포트·userinfo·공백·잘못된 ID는 거절한다. 재생목록·파일 업로드·다른 플랫폼은 지원하지 않는다. 시간 위치 등 부가 query는 분석 URL에 보존하지 않는다.

다운로드 전 metadata로 공개 상태·연령 제한·양의 길이·기본 180초 상한·실시간 여부를 검사한다. URL 검사는 접수 전 수행하지만 metadata 조건 실패는 접수 뒤 job 실패로 나타날 수 있다. **실제 Shorts 분류와 한국어 발화 여부를 완전히 검증하지는 않는다.** 제품의 지원 목표와 구현된 검사 범위를 혼동하지 않는다.

### 접수 응답

```json
{
  "job_id": "0123456789abcdef0123456789abcdef",
  "status": "queued",
  "session_id": "fe-session-example",
  "deduplicated": false
}
```

`status`는 접수·재사용 시점의 상태다. 곧바로 워커가 시작하거나 기존 작업을 재사용하면 queued만 오지는 않는다. 조회는 job_id로 한다. 같은 URL이 진행 중이면 첫 작업을 재사용하며 현재 중복 키에 모델·자막 등 **옵션은 포함되지 않는다.** 다른 옵션 비교 시험은 첫 작업 종료 후 수행한다. 완료 결과 캐시는 없다.

세션당 활성 작업은 하나다. 다른 URL을 같은 세션으로 접수하면 `429/session_busy`가 될 수 있다. 새 세션 ID를 만들어 제한을 우회하지 않는다. POST 응답을 네트워크 오류로 잃어도 이미 접수됐을 수 있으므로 즉시 자동 재접수하지 않는다.

## 작업 조회

`GET /api/jobs/{job_id}`는 해당 작업의 **전체 스냅샷**을 반환한다. FE는 응답을 새 상태로 교체하며, 이전 배열에 계속 append해서 같은 카드를 중복 생성하지 않는다. claim 전용 ID는 현재 없으므로 같은 job의 카드 배열 순서와 text를 일관되게 관리한다.

| 필드 | 의미 |
| --- | --- |
| `id` | 조회 키. POST의 job_id와 동일 |
| `display_id` | CN-0123-4567 형태의 표시·신고용 ID. 조회 키 아님 |
| `url`, `params` | 정규화 URL과 작업 옵션. 비밀키 필드 아님 |
| `status`, `stage` | 작업 생명주기와 현재 세부 단계. stage는 null 가능 |
| `progress` | 0~1 처리 진행값. 정확도·발언 커버리지 아님 |
| `message` | 단계 안내. 문구를 파싱하지 말고 status/stage 사용 |
| `result` | null 또는 부분/최종 결과. 없는 키를 정상 처리 |
| `error` | null 또는 작업 오류. HTTP 200 조회 응답 안에도 존재 가능 |
| `created_at`, `updated_at`, `started_at` | Unix 초. started_at은 대기 중 null |
| `created_at_iso`, `updated_at_iso` | 서버 로컬 시각 문자열. 현재 offset이 없어 UTC로 단정하지 않음 |
| `elapsed_sec` | 접수 이후 총 경과. 종료 뒤 고정 |
| `queue_wait_sec` | 실제 분석 시작 전 대기 시간 |
| `processing_elapsed_sec` | 분석 시작 후 경과. 대기 중 0 |

단일 작업 조회는 수정안에서 session_id/session_ids를 반환하지 않는다. 날짜를 사용자 시간대로 변환할 때는 `new Date(created_at * 1000)`처럼 epoch를 사용한다. `_iso` 값을 임의로 UTC로 해석하지 않는다.

### 부분 응답 예시

주요 필드만 발췌했다. 초기 result는 null이나 media만 있는 dict일 수 있다. 얼굴 분석 불가가 먼저 도착해도 다른 분석은 계속된다.

```json
{
  "id": "0123456789abcdef0123456789abcdef",
  "display_id": "CN-0123-4567",
  "status": "partially_completed",
  "stage": "transcribing",
  "progress": 0.45,
  "message": "발언 텍스트 확보 중...",
  "result": {
    "media": {"title": "예시 영상", "duration": 140.0},
    "face_manipulation": {
      "status": "unavailable",
      "status_label": "분석 불가",
      "detail": "얼굴 기반 분석을 수행하지 못했다.",
      "evidence": [],
      "signals": {}
    }
  },
  "error": null
}
```

### 최종 부분 완료·실패 예시

아래도 주요 필드 발췌다. 나머지 조회 필드는 위 표와 [응답 모델](../backend/schemas.py)을 따른다. Swagger의 queued/partial/limited/failed 예시는 모의 데이터다.

```json
{
  "id": "0123456789abcdef0123456789abcdef",
  "status": "completed_with_limitations",
  "stage": "verifying",
  "progress": 1.0,
  "result": {
    "analysis_status": "partial",
    "face_manipulation": {"status": "inconclusive", "status_label": "판단 보류"},
    "whole_video_generation": {"status": "unavailable", "detail": "전용 모델 미선정"},
    "claim_verification": {"status": "no_claims", "claims": [], "summary": {"total": 0}}
  },
  "error": null
}
```

다음 다운로드 실패도 **작업 조회 HTTP는 200**이며 job.status가 실패다. failed라고 result가 반드시 null인 것은 아니다. 이미 확보한 metadata나 분석 불가 설명이 남을 수 있다.

```json
{
  "id": "0123456789abcdef0123456789abcdef",
  "status": "failed",
  "stage": "collecting",
  "progress": 0.05,
  "result": {},
  "error": {
    "code": "download_failed",
    "message": "영상을 받지 못했습니다. 공개 상태와 지원 조건을 확인해주세요.",
    "retryable": true,
    "stage": "download"
  }
}
```

## 작업 상태와 폴링

| job.status | 화면 의미 | 다음 GET |
| --- | --- | --- |
| `queued` | 대기열에서 기다리는 중 | 계속 |
| `processing:collecting` | 다운로드·프레임·미디어 처리 | 계속 |
| `processing:transcribing` | 발언 텍스트 확보 | 계속 |
| `processing:extracting_claims` | 주장 추출 | 계속 |
| `processing:verifying` | 근거 조회·주장 검증 | 계속 |
| `partially_completed` | 분석 블록·카드 도착. 개별 완료 여부 별도 확인 | 계속 |
| `completed` | 제공 가능한 필수 분석 완료 | 중지 |
| `completed_with_limitations` | 결과는 있으나 일부 실패·누락·미완료 | 중지, 한계 표시 |
| `timed_out` | 처리 시간 초과로 종료 | 중지, 남은 결과·시간 초과 표시 |
| `failed` | 제공할 유효 분석 결과 없이 종료 | 중지, error와 축별 이유 표시 |

partially_completed 이후 status는 그대로여도 stage가 transcribing → extracting_claims → verifying로 갱신된다. 최종 result.analysis_status=complete/partial은 별도 값이다. **폴링 종료는 네 가지 최종 job.status로 판단**하고 progress=1만으로 성공을 판정하지 않는다.

정상 GET은 이전 요청 완료 약 2~3초 후 다음 요청을 보내 겹치지 않게 한다. 이는 현재 연동 예시 간격이지 기획 지연 목표 달성의 실측이 아니다. 404이면 동일 job 폴링을 중지하고 소실·만료 안내 뒤 재분석 여부를 묻는다. 429는 Retry-After를 따르고 없으면 현재 서버 안내값 10초를 사용한다. 500·일시적 네트워크 오류는 횟수 제한과 증가 간격으로 GET만 재시도한다. 인증·CORS 실패는 무한 재시도로 해결하지 않는다.

기본 600초는 실제 분석 시작 후의 시간 예산이다. 실행 중 다운로드·STT를 정확히 그 시각에 강제 중단하는 기능은 없다. FE 타이머가 600초를 넘었다고 성공/실패를 확정하거나 POST를 다시 보내지 않는다. 지연 안내와 조회·이탈 동작을 제공한다.

브라우저 이탈·AbortController는 FE 요청·폴링만 중단한다. 서버 작업 취소 API는 없다. job은 메모리 보관이므로 재시작 시 소실되고 기본 보관 상한 200건에서 오래된 완료 작업이 제거될 수 있다. 고정 TTL·영구 결과 링크·계정 이력은 제공하지 않는다.

## 결과 필드와 표시 규칙

최종 result는 url, media, analysis_status, stages, face_manipulation, whole_video_generation, claim_verification, transcript를 가진다. 진행 중에는 일부 키만 있고 null도 많으므로 기본 대기 상태를 준비한다. 중첩 모델은 Swagger Schemas를 참고하되 예시에 없는 미래 필드를 이유로 파싱 전체를 실패시키지 않는다.

### 미디어·단계

- media: title, uploader, duration(초), video_id, thumbnail, upload_date, language, transcript_source 등의 화면 정보. 없는 날짜·썸네일·언어를 지어내지 않는다.
- stages: download, frames, media_manipulation, transcript, claim_verification, cleanup 등 단계별 `{status, detail, elapsed_sec, error}`. status는 ok/failed/skipped이고 다른 값은 null 가능하다.
- face_manipulation/whole_video_generation: `{status, status_label, detail, evidence, signals}`. status는 suspected/no_clear_signs/inconclusive/unavailable. status_label과 범위·한계를 표시하고 signals 숫자를 조작 확률로 표시하지 않는다.
- 얼굴 미검출·분류기 실패는 정상 영상이라는 뜻이 아니다. 전용 전체 AI 모델은 없으며 unavailable이면 필수 축 누락이다. 기존 자가표기 키워드에도 오탐·유형 구분 한계가 남아 있다.
- cleanup=ok는 그 요청의 자동 생성 임시 디렉토리 정리 결과다. 지정 보존 경로·모델 캐시·강제 종료 후 파일까지 모두 삭제됐다는 보증은 아니다.

### 주장 축과 카드

claim_verification.status는 analyzed/no_claims/unavailable이다. analyzed만으로 모든 카드 완료를 판정하지 않는다. no_claims는 정상적으로 검증 대상을 찾지 못한 경우, unavailable은 발언 확보·추출 실패나 옵션 해제 등으로 검증하지 못한 경우다.

| 카드 필드 | 표시 규칙 |
| --- | --- |
| `text`, `context` | 주장과 원문에서 대조한 문맥. 제목을 외부 근거로 취급하지 않음 |
| `start`, `end`, `time_precision` | 초 단위. exact는 텍스트 매칭 성공이지 STT 시각의 절대 정확도 아님. approx는 "약 00:18", null은 위치 확인 불가 |
| `mentions` | 반복·문맥·위치 부가 정보. 모든 항목에 같은 키가 있다고 가정하지 않음 |
| `status` | pending/verifying/done/failed/timed_out 처리 상태 |
| `verdict`, `verdict_label` | supported/refuted/unverified와 근거와 일치/불일치/근거 부족. done일 때만 최종 판정으로 표시 |
| `reason`, `insufficient_reason`, `insufficient_label` | 판단 이유·부족 사유. 실패를 거짓으로 표시하지 않음 |
| `quote`, `evidence` | 대표 인용과 자료 목록. 아래 검증 상태를 함께 확인 |
| `video_title`, `video_published_at` | 발언 해석용 문맥. 외부 근거 아님 |

summary에는 total/pending/verifying/done/failed/timed_out과 판정별 supported/refuted/unverified가 있다. 판정별 개수는 done 카드만 집계한다. 처리 완료율은 done/total로 표시하되 total=0이면 나누지 않는다. 미완료에는 failed도 포함한다. unverified는 거짓이나 실행 실패의 동의어가 아니다.

부족 사유 코드는 no_source, not_direct, timeout, time_mismatch, source_conflict, weak_source, partial이다. 사용자 문구는 insufficient_label을 우선 사용한다. 제공자 오류로 검색할 수 없었던 결과와 정상 검색 후 자료가 없었던 결과를 구분한다.

### 근거 자료의 신뢰 경계

자료는 title/url/source/publisher/published_at/source_type/source_type_label/is_primary/rating/snippet/content/content_scope/provenance_verified/independence_group/cited/cite_reason/quote 등을 가진다. null·미확인값은 임의로 채우지 않는다.

- content_scope=search_excerpt는 검색 제목·발췌 범위다. 해당 기사의 원문 전체를 읽었다는 뜻이 아니다.
- is_primary·출처 유형은 분류 정보이며 원문 진위 보증이 아니다.
- 일치·불일치에는 서버가 검증한 원문·출처 대응·인용·충분성 조건이 필요하다. 원문 검증 상태는 LLM이나 FE가 생성하지 않는다.
- 현재 실제 검색 제공자는 원문 검증 정보를 확보하지 못한다. 그 자료만으로는 일치·불일치 조건을 통과하지 못하며 참고 자료를 붙이고 유보한다. 모의 원문 테스트를 실제 수집 완료로 읽지 않는다.
- cited=false 자료는 참고 자료로 분리한다. 전문기관 rating을 FE에서 최종 판정으로 승격하지 않는다.
- 외부 문구는 text로 렌더링하고 원시 HTML을 주입하지 않는다. 링크도 HTTP(S)만 허용한다.

### STT 비율은 정확도가 아니다

transcript에는 summary, keywords, tone, language, word_count, source, coverage_pct, coverage_basis, segment_coverage_pct, coverage_detail, signals가 있다.

coverage_pct와 같은 값인 media.stt_coverage_pct는 호환 필드다. basis가 input_audio_duration이면 입력 오디오 길이 비율, caption_last_timestamp이면 마지막 자막 시점 비율이다. 100%여도 발언을 모두 정확히 인식했다는 뜻이 아니다. segment_coverage_pct는 비어 있지 않은 텍스트 구간의 합집합을 영상 길이로 나눈 값이며 역시 정확도·완전성 점수가 아니다. media에는 transcript_coverage_basis, transcript_segment_coverage_pct, transcript_coverage_detail이라는 이름으로 대응 값이 실린다.

## 오류를 세 층으로 구분하기

### HTTP 요청 자체의 오류

```json
{
  "error": {
    "code": "invalid_request",
    "message": "요청 값이 올바르지 않습니다.",
    "retryable": false,
    "fields": [{"field": "body.max_frames", "reason": "허용 범위를 확인해주세요."}]
  },
  "request_id": "example-request-01"
}
```

| HTTP | 대표 코드 | 처리 |
| --- | --- | --- |
| 404 | job_not_found | job 소실·미존재 또는 닫힌 디버그 경로. 동일 ID 폴링 중지 |
| 422 | invalid_request | 필드/형식 오류. error.fields 확인 후 수정 |
| 422 | unsupported_url | URL 입력 수정 필요 |
| 429 | session_busy | 기존 세션 작업 확인. 다른 작업 반복 접수 금지 |
| 429 | server_busy | 대기열 포화·접수 중단·준비 중. Retry-After 사용 |
| 500 | internal_error | request_id·작업 ID로 문의. POST 무한 재시도 금지 |

현재 429 응답은 Retry-After: 10이다. 수정안은 X-Request-ID와 Retry-After를 허용 Origin 브라우저에 노출하지만 Cloudflare 응답에는 이 계약이 없을 수 있다. 오류 본문 request_id도 확인한다. 임의 경로·메서드의 기본 FastAPI 404/405는 위 봉투와 다를 수 있으므로 문서의 정확한 경로를 사용한다.

### 접수 후 작업·분석 단계의 오류

분석은 백그라운드 작업이므로 다운로드·STT가 실패해도 다음 GET 자체는 HTTP 200일 수 있다. job.status, job.error, result.stages, 카드 상태를 읽는다. completed_with_limitations에도 error나 실패 단계가 함께 있을 수 있다.

| 코드 | 주로 나타나는 위치 | 의미 |
| --- | --- | --- |
| unsupported_video | job.error | 길이·공개 상태·연령 등 metadata 조건 거절 |
| download_failed | job.error 또는 단계 오류 | 수집 실패 |
| transcription_failed | stages.transcript.error | STT 실패, 다른 결과는 남을 수 있음 |
| dependency_missing | job.error 또는 단계 오류 | 필요한 모델·라이브러리 사용 불가 |
| internal_error | job.error 또는 단계 오류 | 예상하지 못한 오류의 공개 코드 |
| analysis_unavailable | job.error | 제공할 완료 분석 결과가 없음 |
| server_shutdown | job.error | 서버 종료로 대기 작업 취소 |

오류 클래스에 download/transcription 502, dependency 503, unsupported_video 422 매핑은 있지만 현재 비동기 경로에서는 대개 작업 데이터 안에 저장된다. **그 숫자가 다음 폴링 HTTP status로 반환될 것이라고 가정하지 않는다.** 카드 실패는 error 없이 status·reason·단계 detail로 나타날 수도 있다. retryable은 성공 보증이나 새 분석 자동 실행 지시가 아니다.

### Cloudflare·브라우저 경계의 오류

미인증/만료의 Access 리다이렉트·로그인 HTML, 403, 네트워크 TypeError·CORS 차단은 FastAPI 오류 봉투가 아니다. fetch가 따라간 로그인 응답에 JSON 파싱을 시도하지 않도록 content-type과 형태를 확인한다. 모든 TypeError를 인증 만료로 단정하지 않고 요청·Origin·OPTIONS·연결 오류를 구분한다. 서버 설정 변경은 담당자가 승인 후 수행한다.

## 브라우저 요청 최소 예시

아래는 **개발계 Access 보호 + 승인된 CORS 설정을 전제로 한 예시**다. 복사만으로 Vercel의 제3자 쿠키 제한이나 인증을 해결하지 않는다. 자동 재시도·사용자 안내는 위 상태 규칙과 인수 테스트에 맞춘다.

```javascript
const apiBase = "https://conan-api-dev.dotseven.cloud";
const terminal = new Set([
  "completed", "completed_with_limitations", "timed_out", "failed",
]);

async function apiJson(path, options = {}) {
  const response = await fetch(`${apiBase}${path}`, {
    ...options,
    credentials: "include",
  });
  const isJson = (response.headers.get("content-type") || "")
    .includes("application/json");
  if (response.redirected || !isJson) {
    throw new Error("API JSON이 아닙니다. Access 로그인과 연결 상태를 확인해주세요.");
  }
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error?.message || "API 요청 실패");
    Object.assign(error, {
      httpStatus: response.status,
      code: data.error?.code,
      requestId: data.request_id || response.headers.get("x-request-id"),
      retryAfter: response.headers.get("retry-after"),
    });
    throw error;
  }
  return data;
}

async function submitVideo(videoUrl, sessionId) {
  return apiJson("/api/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: videoUrl, session_id: sessionId }),
  });
}

async function pollOnce(jobId, signal) {
  const job = await apiJson(`/api/jobs/${encodeURIComponent(jobId)}`, { signal });
  // result가 null/부분 dict여도 안전하게 렌더링한다.
  return { job, finished: terminal.has(job.status) };
}
```

pollOnce는 직전 요청 완료 후 예약하고 finished=true, 404, 화면 이탈에서 예약을 해제한다. 오류 처리기를 두어 무한 루프·중복 POST를 만들지 않는다. 실제 분석은 [FE 인수 체크리스트](frontend-integration.md#fe-인수-체크리스트)와 테스트 승인을 확인한 뒤 실행한다.
