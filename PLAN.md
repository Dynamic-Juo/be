# DeepCheck — AI 가짜영상 위험도 분석 서비스 (기획서)

> 레포: `/Users/najeong-gyun/Developer/opencode/deepcheck`
> 작성일: 2026-09-05 · 단계: MVP 동작 완료 + 탐지 갭 확인 + 멀티세션 하네스 설계

---

## 1. 개요

**문제 정의:** 요즘 AI가 만든 가짜(딥페이크) 영상/뉴스가 넘친다. 유저가 영상 링크를 붙이면
**그 영상이 AI가 만들어낸 것인지 위험도를 0~100으로 판정**해주는 검증 서비스.

**제품 기능(핵심):**
1. 링크를 받아 **영상/오디오 다운로드**
2. **음성 → 텍스트** 분석 (내용 요약, AI음성/클릭베이트 신호)
3. **AI가짜영상 여부 탐지** (ViT 분류기 + VLM + 휴리스틱)
4. **위험도 레포트** (0~100 + 판정 + 근거)

- **프론트**: 별도 담당자가 `result` (JSON)를 받아 UI로 표시
- **백엔드**: REST API (FastAPI, job 기반)  ← 우리 담당

---

## 2. 파이프라인 아키텍처 (4단계)

```
URL ─┬─① 다운로드─┬─② STT─(text)─┬─④ 위험도 ──► 레포트
     │  yt-dlp    │  faster-whisper  │ (text/json/html)
     │            │   + analyzer     │
     ├─(영상)─③ ──┼─(frames)─────────┤
     └─(오디오)    ViT 분류기 + 휴리스틱 + VLM
```

| 단계 | 모듈 | 도구 | 출력 |
|---|---|---|---|
| ① 다운로드 | `downloader.py` | `yt-dlp` + `av`(PyAV) | 영상(mp4) + 오디오(m4a) 분리, 메타 |
| ② 텍스트 | `transcriber.py`→`analyzer.py` | `faster-whisper`(ctranslate2) | 요약/키워드/tone + tts/clickbait/claim 점수 |
| ③ 가짜영상 | `deepfake.py` | ViT(`dima806`) + 옵션 VLM + 휴리스틱 | avg_fake_score 0~100, 프레임별 근거 |
| ④ 레포트 | `report.py` | — | `final_risk`, `verdict`, `evidence` |

**왜 "영상/오디오 분리"?** 유튜브는 JS런타임/ffmpeg 없이 결합영상이 없어서, 각 스트림을 따로 받아
프레임(영상)·STT(오디오)에 분리 사용. PyAV 번들로 시스템 ffmpeg 불필요.

---

## 3. 백엔드 — Hermes 하네스 (멀티세션/멀티쓰레드)

분석은 무거워서 이벤트 루프에서 못 돌리고, 요청마다 무한 스레드를 만들면 안 된다.
따라서 **bounded worker pool + backlog 큐 + 세션** 구조로 설계 (파일: `backend/harness.py`).

```
submit(url, session_id) ──► [bounded queue] ──► [N workers] ──► job 상태/결과
        ▲                      (backpressure)      (concurrency)
        │                          │  queue.Full → HTTP 429          │
        └──── JSON: {job_id, session_id, deduplicated} ◄─────────────┘

GET /api/jobs/{job_id}      → status/progress/result
GET /api/sessions/{sid}     → 그 세션이 요청한 job 목록 (프론트 세션별 그룹핑)
GET /api/jobs               → 전체 (디버그)
GET /health                 → {workers, backlog_size, inflight_urls, status.counts}
```

### 동시성/세션 설계 포인트
- **Worker pool**: `DEEPCHECK_WORKERS`(기본 3)개 워커(Thread)가 큐에서 잡아 실행 → 동시 요청 병렬 처리
- **Backpressure**: backlog를 `maxsize`로 제한 → 넘치면 `429` 반환(클라이언트 재시도). 폭주 방지
- **멀티세션**: job에 `session_id` 태그. 프론트는 자기 세션 job만 조회 가능
- **URL Dedup**: 같은 URL이 queued/running이면 중복 분석 대신 **같은 job 재사용** (여러 세션이 같은 링크를 제출해도 1회만 분석)
- **Graceful**: VLM/분류기 없으면 자동 폴백. 헤비단계(STT)만 병목이고 나머지는 가벼움

---

## 4. 현재 상태 (MVP)

- [x] 파이프라인 4단계 동작 (CLI `python -m deepcheck.cli analyze <url>`)
- [x] FastAPI `POST /api/analyze` + `GET /api/jobs/{id}` job polling
- [x] Hermes 하네스 (워커풀 + 큐 + 세션 + dedup) — 병렬·듀프 검증 완료
- [x] 출력 text/json/html
- [ ] AI생성영상 탐지 성능 보강 (아래 갭)
- [ ] 검색엔진 느낌 프론트 (프론트 담당)

### 검증 결과
| 테스트 | 결과 | 코멘트 |
|---|---|---|
| 실사영상 (zoo, 19s) | `final_risk 0.6` "진짜 높음" | ✅ 정상 판단 |
| **AI 표기 영상 (호날두 패러디, 10s)** | `final_risk 0.5` "진짜 높음" | ❌ **탐지 실패** |

### 탐지 갭 분석 (중요)
제목에 *"AI 기술로 제작된 가상의 패러디 영상"* 인데 `frames_fake 0/8`로 못 잡음. 원인:
1. **`dima806`는 옛 '얼굴스왑' 딥페크용** → Sora/Veo 등 신형 AI생성 영상 분포와 다름 → 진짜로 오판
2. **프레임 샘플링 희석**: 8장 평균이라 국소 흔적이 감소
3. **자가표기 미사용**: 제목/설명의 "AI/가상/패러디" 키워드를 안 읽음 (큰 신호)
4. **VLM 비활성**: 시각 사유로 "얼굴이 AI인지" 본질 판독 가능하나 꺼져 있음

---

## 5. 개선 로드맵

### Tier 0 (가성비 최고, 1~2시간)
- [ ] 제목/설명 **자가표기 신호**: "AI/가상/패러디/생성/딥페이크" 키워드 → 텍스트 위험도 상승
- [ ] `analyzer`에 제목·설명·(STT) 훅 추가 → 즉시 위 표기영상 잡힘

### Tier 1 (AI생성 분류기 보강)
- [ ] **AI생성 이미지 분류기 추가/교체**: `dima806`(딥페크) 대신 `Organika/sdxl-detector` 등 GenAI 생성물 판별 모델. 로컬 무료
- [ ] 모델 앙상블: 딥페크스코어 + AI생성스코어 병렬 → max/가중
- [ ] 프레임 전략: 짝수(1s마다) 샘플링 + 이상 징후 프레임 자동 upweight

### Tier 2 (시각 사유 강화)
- [ ] Ollama `qwen2.5vl` VLM 활성화 → "왜 AI로 보이는지" 근거문 evidence 추가. `brew install ollama`
- [ ] (선택) 오디오에서 AI TTS(합성음) 감지 — 외부 모델 or STT 신뢰도 활용

### Tier 3 (서비스화)
- [ ] API Gateway/rate-limit, 결과 캐시(영상 hash → result)
- [ ] Front: 검색엔진 느낌 입력+결과 카드 대시보드
- [ ] 배포 Dockerfile (uvicorn + worker), 모델 프리다운로드

---

## 6. 기술 스택 & 비용

| 단계 | 라이브러리 | 비용 | 비고 |
|---|---|---|---|
| 다운로드 | `yt-dlp`, `av` | 무료 | 로컬 |
| STT | `faster-whisper`→`ctranslate2` | 무료 | 로컬 |
| 이미지/프레임 | `pillow` | 무료 | 로컬 |
| 딥페크 분류 | `torch`,`torchvision`,`transformers`,`timm` | 무료 | 로컬 |
| VLM(옵션) | Ollama `qwen2.5vl` | 무료 | 로컬 |
| 웹 | `fastapi`,`uvicorn` | 무료 | 로컬 |

**전부 로컬·무료, API키 없음.** 유일 필수: 인터넷(영상 다운로드 + 모델 1회 다운로드).
`HF_TOKEN` 미설정 시 다운로드가 느릴 뿐(레이트리밋), 비용 아님.

### 의존성 파일
- `requirements.txt` — 코어
- `requirements-vision.txt` — ViT 분류기 (선택)
- `requirements-backend.txt` — FastAPI (선택)

---

## 7. 성능 / 커버리지

- 시간 지배: **STT(오디오 전체 전사)** + 다운로드(네트워크). 프레임/분류기는 영상길이와 무관.
- 기준: `tiny`/`base` 모델, Mac M칩 int8 → 음성보다 빠름(≈2~5x).
- 체감:
  - 30초 이하 → ~20~40초
  - 2~3분 → ~40~90초
  - 5~10분 → ~1.5~4분 (데모 한계선)
  - 10분 이상 → 비추천 (STT+다운로드 증가)
- **데모 최적**: 1~3분 이하 영상 + `model_size:"tiny"`, `max_frames: 8`.
- 스레드: `DEEPCHECK_WORKERS` 로 병렬도 조절.

---

## 8. 테스트 / 확인 방법

```bash
cd /Users/najeong-gyun/Developer/opencode/deepcheck
source .venv/bin/activate

# 1) CLI 한번에
python -m deepcheck.cli analyze "URL" --model-size tiny --max-frames 8 --with-classifier

# 2) API 서버
uvicorn backend.app:app --host 0.0.0.0 --port 8000
# Swagger → http://127.0.0.1:8000/docs

# 3) 요청/폴링
curl -X POST http://127.0.0.1:8000/api/analyze \
  -H 'Content-Type: application/json' \
  -d '{"url":"URL","model_size":"tiny","max_frames":8,"session_id":"SES_A"}'
→ {job_id, session_id, deduplicated}
curl http://127.0.0.1:8000/api/jobs/{job_id}   # status/progress/result
curl http://127.0.0.1:8000/api/sessions/SES_A  # 세션별 job 목록
```

**result 스키마 (프론트 계약):** `final_risk`, `verdict`, `video_risk`, `text_risk`,
`summary`, `keywords`, `deepfake{frames_analyzed, frames_fake, method, evidence, vlm_summary}`,
`text{tts_risk, clickbait_risk, claim_risk, tone}`, `evidence[]`, `title/uploader/duration/language`.

---

## 9. 다음 우선순위 (제안)

1. **Tier 0** — 제목/설명 자가표기 신호 (오늘 끝낼 수 있는 고수익, 위 영상 바로 잡힘)
2. **Tier 1** — AI생성 분류기 추가 → "실제 AI영상" 감별력 확보 (데모의 신뢰 포인트)
3. **Tier 2** — VLM 사유 → 근거문
4. 그 후 프론트 연동 계약 확정 & 배포

---

## 10. 패치 로그 (2026-09-05, 코드 리뷰 후 적용)

레포 코드를 직접 열어서 검토한 뒤 아래 5개를 실제로 패치하고 시뮬레이션으로 검증 완료:

1. **`deepfake.py` VLM 옵트인 버그 수정** — `_vlm_analyze` 가드 조건이 잘못돼서, 백엔드 기본 요청(`use_classifier=True, vlm_model=None`)마다 VLM이 "옵션"인데도 매번 Ollama를 호출 시도하던 문제. `vlm_model`이 명시된 경우에만 실행하도록 수정.
2. **`downloader.py` 프레임 샘플링 버그 수정** — ffmpeg가 없을 때 쓰이던 PyAV 폴백(`_frames_pyav`)이 "영상 앞부분 N프레임"만 뽑고 있던 문제. 영상 길이 기준으로 균등 분산 샘플링하도록 재작성 (ffmpeg 있을 때 경로와 동일한 방식).
3. **`analyzer.py` — 제목/설명 자가표기 신호 추가 (Tier 0)** — `detect_self_disclosure()` 신규 추가. "AI로 제작", "패러디", "딥페이크" 등 표기를 제목/설명에서 감지해 `self_disclosure_risk`/`self_disclosure_evidence`로 노출. `TextSignals.risk`를 죽은 `@property`(asdict에 안 잡힘)에서 실제 필드로 변경 — 기존엔 report.py가 같은 가중치 공식을 따로 한 번 더 계산하던 중복 코드였음.
4. **`cli.py`** — `analyzer.analyze()` 호출에 `media.title`/`media.description`을 실제로 넘기도록 수정 (기존엔 메타데이터 표시용으로만 쓰고 위험도 계산엔 전달 안 하고 있었음).
5. **`report.py` — 자가표기 신호가 최종 점수에 실제로 반영되도록 플로어 로직 추가** — 3~4번만으로는 `final = video*0.65 + text*0.35` 가중치에 희석돼서 최종 점수가 문턱값을 못 넘을 수 있었음 (시뮬레이션으로 확인). `self_disclosure_risk >= 50`이면 `final = max(final, self_disclosure_risk*0.9)`로 하한선을 둬서, "AI로 제작됨"이라고 명시된 영상은 영상 분류기 성능과 무관하게 확실히 잡히도록 수정.

**검증 결과 (시뮬레이션):** PLAN.md 4장에 기록된 "AI 표기 영상(호날두 패러디)" 케이스를 그대로 재현 — 영상 분류기가 여전히 0/8 fake로 못 잡아도, 제목/설명의 자가표기만으로 `final_risk=90.0`, `verdict="AI가짜영상 의심 매우 높음"` 확인. 반면 자가표기 없는 일반 영상은 플로어 로직이 발동하지 않음을 확인.

전체 5개 파일 `python -m py_compile` 통과 확인.

### GenConViT (TrueMedia.org) 평가 결과 — 이번엔 채택 보류
제미나이가 제안한 `truemediaorg/GenConViT`를 실제로 설치/비교 테스트해보려 했으나:
- 가중치가 Hugging Face에 호스팅돼 있는데, 지금 이 세션(클라우드 컨테이너 + 로컬 기기 셸 둘 다)에서 조직 네트워크 정책상 `huggingface.co` 자체가 막혀 있어 다운로드 불가 확인 (본인 맥에서 터미널로 직접 돌리면 이 제약과 무관할 가능성 높음 — 세션 프록시만의 제약).
- **라이선스가 GPL-3.0** — 지금 스택은 MIT/BSD 위주라 그대로 라이브러리로 링크하면 프로젝트 전체 라이선스 정책에 영향 줄 수 있음.
- 저장소가 "더 이상 유지되지 않고 PR/이슈 미수용" 상태 + `dlib`/`decord` 같은 무겁고 빌드 까다로운 의존성.
- **결론:** 정확도 수치(95.8%)는 매력적이지만 라이선스·유지보수·의존성 리스크가 해커톤 타임라인엔 안 맞음. Tier 1은 기존 추천대로 `lynote-ai/ai-image-detector`(MIT, 가벼움, 활발히 유지)로 유지 추천. GenConViT는 "watch list"로만 남겨둠.

---

## 11. 패치 로그 2 (2026-09-05, Tier 1 착수 — 얼굴 crop + 모델 캐싱)

1. **`deepcheck/face.py` 신규** — MediaPipe Tasks API로 얼굴 detection 후 padding 포함 crop. 모델 파일(`blaze_face_short_range.tflite`, ~1-3MB)이 로컬에 없으면 조용히 `None`을 반환해 기존 전체 프레임 분석으로 폴백 (다른 옵션 스테이지들과 동일한 degrade 패턴). **주의**: 최신 mediapipe(0.10+)는 예전 `mp.solutions` 번들 API를 없애서 모델 파일을 별도로 받아야 함 — README.md에 1회성 다운로드 커맨드 추가.
2. **`deepfake.py`** — `DeepfakeDetector.analyze()`에서 분류기에 넣기 전에 `face.crop_face()`로 얼굴 crop 시도 → 성공하면 crop을, 실패(얼굴 없음/모델 없음)하면 원본 프레임을 분류기에 입력. `FrameResult.face_cropped`로 crop 적용 여부를 evidence에 노출.
3. **모델 재로딩 문제 수정 (중요, 배포 관련)** — `transcriber.py`의 `WhisperModel`과 `deepfake.py`의 ViT 분류기 파이프라인이 **분석 요청마다 매번 새로 로드**되고 있던 걸 발견. 프로세스당 1회만 로드해서 재사용하는 module-level 캐시(스레드락 포함)로 수정. 맥미니처럼 리소스 제한된 곳에 배포할 때 특히 중요 — 동시 요청마다 모델을 중복 로드하며 메모리를 먹던 문제 해결.

**검증**: 모의(mock) 의존성으로 캐싱 로직 단위 테스트 3개, `DeepfakeDetector.analyze()` 전체 파이프라인 end-to-end 스모크 테스트 통과 (합성 이미지 3장, 얼굴 crop 모델 없는 상태 + 분류기 다운로드 불가 상태에서도 크래시 없이 heuristics 폴백으로 정상 완료 확인). 단, **얼굴 검출이 실제로 잘 되는지(진짜 얼굴 이미지 기준)는 이번 세션에선 검증 못 함** — 모델 파일 호스트(storage.googleapis.com)가 이 세션의 클라우드/기기 셸 양쪽 다 조직 정책으로 차단돼 있어서 다운로드 자체가 안 됨. 로컬 맥에서 직접 `curl`로 받으면(README 참고) 문제없을 가능성 높음 — 받은 후 직접 실행 확인 필요.

전체 `python -m py_compile deepcheck/*.py backend/*.py` 통과.

---

## 12. 첫 실제 실행 결과 (2026-09-05, 니 맥에서 직접 실행)

`https://www.youtube.com/watch?v=XQr4Xklqzw8` ("AI Will Smith eating spaghetti pasta") 실제 다운로드+STT까지 실행 확인. 결과: `final_risk 63/100, "상당함"` (영상 heuristics는 0/100, `--with-classifier` 미사용).

**63점의 정체**: 자가표기 신호가 발동한 것으로 확인 (`63 = 70(자가표기 점수) * 0.9(플로어 계수)`, 70 = 55 + 15*1개 매치). 제목엔 "AI footage"라고만 돼있어 키워드 목록에 안 걸렸을 걸로 예상했는데, 영상 설명(description)에 있던 문구가 매치된 것으로 보임(정확한 문구는 `--json`으로 재확인 필요).

**이 과정에서 발견한 버그**: 이 자가표기 감지 결과가 텍스트/HTML 리포트에는 안 보이고 `--json`에만 들어있던 문제 발견 → `report.py`의 `format_text`/`format_html`에 "[텍스트] 자가표기 감지" 줄 추가해서 수정.

---

## 13. 영상 위험도 평균-희석 문제 수정 (2026-09-05, 실제 테스트로 발견)

`--with-classifier` 첫 실제 실행에서 frame_000이 98.4% fake로 잡혔는데도 나머지 7프레임 평균에 묻혀 최종 영상 위험도가 13/100까지 떨어지는 걸 실측으로 확인. `DeepfakeDetector.analyze()`의 평균 계산을 "평균과 최댓값의 가중 블렌드(avg*0.6 + peak*0.4, 둘 중 큰 값)"로 수정 — 동일 상황 재현 시 13.1 → 47.2로 상승 확인. 가중치(0.6/0.4)는 임의 선택값이라 추후 실측 데이터로 튜닝 필요.


## 14. 단계적 파이프라인 검증 계획 (2026-09-05)

AI 판독 정확도를 파려고 하기 전에, 그 앞단(다운로드 → STT)이 실제로
길이/내용 손실 없이 되는지부터 단계적으로 확인하기로 함.

### 요약 로직 확인 결과
`analyzer.summarize()`는 LLM/외부 API 호출이 아니라 순수 로컬 룰베이스
알고리즘임: 문장 분리 → 키워드 빈도 스코어링 → 상위 3문장 추출 → 300자
컷. 즉 "요약본을 받는" 게 아니라 우리가 직접 만든 간단한 추출 요약.

### 발견: 전체 transcript가 저장되지 않고 있었음
`cli.py`의 `analyze_url()`은 STT 전체 텍스트를 로컬 변수로만 들고 있다가
`analyzer.analyze()`에 넘겨 요약/신호만 뽑아내고, 함수가 끝나면 원문은
버려짐. 최종 리포트(JSON/HTML)에는 300자 요약만 남고 원문 전체는 디스크
어디에도 남지 않는 구조였음.

### 패치: `--save-transcript` 옵션 추가 (커밋 53aa6c6)
- STT 처리 직후 원문 전체 + 타임스탬프 세그먼트를 지정 파일로 저장
  (`--save-transcript` 또는 `--save-transcript path.txt`, 기본
  `deepcheck_transcript.txt`).
- STT가 실제로 영상 끝까지 처리했는지 확인할 수 있도록
  "STT 커버리지: 처리시간s / 영상길이s (%)" 로그를 stderr에 추가.
- 코드 레벨에서 STT 자체에 길이 제한/타임아웃은 없음(`transcriber.py`가
  세그먼트 전체를 순회). 유일한 위험 지점은 `downloader.extract_audio()`의
  선택적 ffmpeg 변환 서브프로세스에 걸린 `timeout=600`(10분)인데, 이건
  시스템 ffmpeg가 설치돼 있고 오디오 변환 자체가 10분을 넘는 아주 긴
  영상(수 시간 단위)에서만 실제 문제가 됨. yt-dlp 다운로드 자체에는
  길이/용량 상한이 코드에 없음 — 즉 이론상 제한은 없지만 안전장치도 없음.

### 검증 순서 (합의됨)
1. **다운로드 확인** — 긴 영상(예: 20~30분 이상) 대상으로 `--keep`으로
   실행해서 워크dir에 남은 video/audio 파일 실제 재생시간이 원본과
   일치하는지 확인.
2. **STT 확인** — 같은 실행에 `--save-transcript`를 같이 줘서 커버리지 %
   로그와 저장된 transcript 파일의 마지막 타임스탬프가 영상 끝까지
   닿는지 확인.
3. 1, 2가 모두 통과된 뒤에만 AI 판독(딥페이크 분류기) 튜닝/추가 검증을
   진행하기로 함 — 순서 역전 금지.
