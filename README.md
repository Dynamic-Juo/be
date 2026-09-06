# DeepCheck — AI fake video detector

YouTube/any web video 링크를 넣으면 → **다운로드 → 텍스트(STT) 분석 → AI가짜영상 탐지 → 위험도 레포트** 를 한번에 주는 CLI 툴입니다.
AI 헤커톤 데모용 파이프라인입니다.

## 파이프라인 (3단계)

1. **다운로드** — `yt-dlp`로 영상/오디오 획득 (yt-dlp만으로도 ffmpeg 없이 PyAV로 디코딩 가능)
2. **텍스트 분석** — `faster-whisper`(CTranslate2) STT → 내용 요약 + 클릭베이트/AI음성 신호 분석
3. **가짜영상 탐지** — 프레임 샘플링 → 딥페이크 이미지 분류기(ViT) + VLM 사유 + 휴리스틱 → AI 의심 점수
4. **위험도 레포트** — 위 신호들을 가중합산해 0~100 위험도 + 근거를 출력

## 설치

```bash
cd deepcheck

# Python 3.12 권장 (torch/ctranslate2 휠이 안정적). uv 사용 시 자동 설치 가능
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -r requirements.txt
```

### 딥페이크 frame 분류기 (선택이지만 강력하게 권장)
`--with-classifier` 로 ViT 딥페이크 분류기를 쓰려면 vision 스택을 설치하세요:
```bash
uv pip install -r requirements-vision.txt
```

### ffmpeg (선택)
faster-whisper의 PyAV과 우리의 PyAV 프레임 추출이 대부분 디코딩을 해결하므로 ffmpeg는 필수는 아닙니다.
더 고품질 변환/다양한 포맷이 필요하면 설치:
```bash
brew install ffmpeg
```

### VLM 사유 (선택)
Ollama에서 `qwen2.5vl:7b` 등 비전모델을 띄우면 영상의 `AI 생성 흔적`을 자연어로 분석해 근거문으로 추가합니다.
```bash
ollama pull qwen2.5vl:7b
```

### 얼굴 crop 모델 받기 (선택이지만 강력하게 권장)
`--with-classifier`가 쓰는 딥페이크 분류기는 원래 얼굴만 crop된 이미지로 학습된 모델이라,
전체 프레임을 그대로 넣으면 정확도가 떨어집니다. `deepcheck/face.py`가 MediaPipe로 얼굴을
찾아 crop해주는데, 최신 mediapipe(0.10+)는 예전과 달리 모델 파일을 pip 패키지에 안 담고
있어서 최초 1회만 따로 받아둬야 합니다 (약 1-3MB, 네트워크 차단 환경이 아니면 바로 됩니다):
```bash
mkdir -p deepcheck/models
curl -L -o deepcheck/models/blaze_face_short_range.tflite \
  https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite
```
파일이 없어도 에러 없이 얼굴 crop만 건너뛰고 기존처럼 전체 프레임으로 분석합니다.

## 사용법

```bash
# 한번에 전체 분석
python -m deepcheck analyze "https://www.youtube.com/watch?v=..." --model-size small

# JSON 결과만
python -m deepcheck analyze "URL" --json > result.json
```

### 주요 옵션
```
--model-size {tiny,base,small,medium,large-v3}   faster-whisper 모델 크기 (기본 small)
--max-frames N         영상에서 샘플링할 프레임 수 (기본 8)
--with-classifier      ViT 딥페이크 분류기 사용 (torch 필요)
--vlm {ollama,...}     VLM 사유 모드 (qwen2.5vl 등)
--keep                 다운로드한 파일 유지
--json                 머신 리더블 출력
```

## 결과 예시

```
제목: ...  | 길이: 04:23 | 언어: ko (99.3%)
──────────────────────────────────────────────
[음성] 텍스트 요약: ...
[음성] TTS/AI 음성 의심: 사소한    상호작용 징후 점수 12/100
[영상] AI 생성 의심: 상당함       (8프레임 중 6개 fake, 71.2%)
[영상] VLM 근거: "얼굴 표정 미세부자연, 손가락 관절 왜곡 관찰됨"
──────────────────────────────────────────────
[위험도] ████████░░  78/100  → AI가짜영상 의심 높음
```

## 백엔드 API (FastAPI)

프론트가 호출할 REST API 입니다. 분석이 무거우니 **job(polling) 방식**으로 동작합니다.

```bash
uv pip install -r requirements-backend.txt
uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

### 엔드포인트 (`/docs` 에서 Swagger 확인 가능)
| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/analyze` | `{url, model_size, max_frames, use_classifier, vlm_model}` → `{job_id}` 즉시 반환, 백그라운드 분석 |
| GET | `/api/jobs/{job_id}` | `{status, progress, message, result}` — `status`: queued/running/done/error |
| GET | `/health` | 서버 상태 |

### 프론트 연동 예시
```js
let job = await fetch("/api/analyze", {method:"POST", headers:{"Content-Type":"application/json"},
  body: JSON.stringify({url: "https://youtu.be/..."})}).then(r=>r.json());

// 2초마다 폴링
const timer = setInterval(async ()=>{
  const s = await fetch(`/api/jobs/${job.job_id}`).then(r=>r.json());
  render(s.status, s.progress, s.message);
  if(s.status==="done"){ clearInterval(timer); showResult(s.result); }
}, 2000);
```
`result` = 위험도(`final_risk`, `verdict`) + `deepfake`(영상) + `text`(텍스트) + `evidence`(근거).

## 리포 구성
```
deepcheck/
├── deepcheck/
│   ├── downloader.py    # yt-dlp 다운로드(영상/오디오 분리)
│   ├── transcriber.py   # faster-whisper STT
│   ├── deepfake.py      # 딥페이크/AI 탐지 (분류기 + VLM + 휴리스틱)
│   ├── analyzer.py      # 텍스트/클릭베이트/음성신호 분석
│   ├── report.py        # 위험도 합산 + 출력
│   └── cli.py           # CLI + 파이프라인 (analyze_url)
├── backend/
│   └── app.py           # FastAPI 백엔드 (job 기반)
├── requirements.txt       # 코어
├── requirements-vision.txt # ViT 분류기 (선택)
└── requirements-backend.txt # FastAPI (선택)
```

> 주의: yt-dlp는 사이트 약관/저작권에 따라 사용하세요. 학습/리서치 기준 로직으로 데모를 단순화했습니다.
