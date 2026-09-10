# 다음 기기·에이전트를 위한 현재 상태

기준일: 2026-09-11. 맥미니에 main 472aff7 이미지로 conan-staging을 기동했다. /health·/ready·얼굴·ViT·Whisper 로딩과 conan-api-dev.dotseven.cloud의 Tunnel·본인 이메일 한정 Access 연결을 확인했다. 미인증 HTTPS는 302이며 기본 DNS 해석·본인 로그인 후 응답·키 입력·실제 분석은 추가 확인이 필요하다. 실제 배포 상태와 -p conan-staging 명령은 [배포 점검 기록](deployment-log.md)을 먼저 확인한다. 과거 작업 로그는 당시 기록이며 최신 상태를 덮어쓰지 않는다.

## 먼저 읽을 것

최신 실측: 사용자 영상 cYRkZmBuDqI는 내부 API에서 25.1초에 completed였다. 다운로드/미디어 실행은 확인했지만 STT 3단어·coverage 100%와 no_claims 결과로 내용 품질은 미검증이다. 배포 키 세 개가 없어 DeepSeek·NAVER 성공도 미검증이다. 상세는 worklog의 사용자 지정 영상 실측을 확인한다. DNS는 모든 직접 resolver 질의 성공/새 도메인의 macOS 조회 실패로 로컬 캐시·resolver 경로가 의심되며 아직 해결하지 않았다.

추가 상태: `.github/workflows/backend-ci.yml`에 ARM64 이미지 빌드·격리 테스트·main GHCR 게시 초안을 준비했다. 원격 Actions 실행·게시 권한과 맥미니 자동 배포는 미검증이다. 운영 hostname·공개 범위 및 FE Origin을 정한 뒤 환경 분리와 인증/CORS를 연결한다.

공개 범위는 일반 사용자 서비스로 확정됐으나 실제 운영 공개는 보류한다. 프론트가 나오기 전 준비와 팀장님에게 받을 정보는 [프론트 연동](frontend-integration.md)에 정리했다. Compose 환경 파일·네트워크 별칭 분리를 정적으로 검증했다. 기존 이미지의 로컬 테스트는 167개 통과했지만 종료 뒤 백그라운드 다운로드와 닫힌 로그 스트림 오류가 있어 테스트 작업 종료 격리를 보완해야 한다. 서버 `.env.home` 키 세 개는 재확인 시에도 비어 있었고 DNS 일반 요청도 실패했다.

1. [작업 규칙](../AGENTS.md)과 이 문서
2. [기획 PR #6](https://github.com/Dynamic-Juo/docs/pull/6), 특히 PR 머리의 PRD·evidence-policy·analysis-runtime·result-ui와 최신 리뷰
3. [프롬프트 평가](prompt-evaluation.md), [코드 흐름](pipeline.md)
4. [맥미니 배포](deployment-mac-mini.md), [작업 로그](worklog.md)

기획 검토 기준은 docs의 `origin/docs/mvp-review` 커밋 `35df85c`였다. docs/main이나 과거 평가 문서의 '미구현'만 보고 현재 기능을 판단하지 않는다. PR 본문·커밋·후속 댓글을 구분한다.

## 저장소와 공유 상태

| 저장소 | 역할과 기준 |
| --- | --- |
| docs | 팀 기획·결정·전체 평가. 변경 제안은 팀의 문서 운영 규칙을 따른다. |
| be | 현재 구현·테스트·실행 문서. DeepSeek 주장 추출·판정, NAVER API HUB 뉴스·백과 검색이 이미 구현됐다. |
| playground | 백엔드를 시작하기 위해 만든 초기 실험 저장소. 현행 API의 기준이 아니다. |
| fe | 아직 저장소·배포 주소 미확인. 조정준 팀장이 와이어프레임을 기준으로 만들고 Vercel에 배포할 예정이다. |

2026-09-10 재확인한 로컬·원격 main은 `472aff7201a834102d6ec2c27096dabb22a5ae4f`다. `fix/prompt-handoff`는 PR #1로 main에 병합됐으며 DeepSeek·NAVER·배포 구성도 main에 포함된다. 과거 미공유·병합 대기 기록은 worklog에 당시 이력으로 보존한다.

현재 서버 점검 문서 브랜치는 main에서 분기한 `docs/mac-mini-readiness`다. 서버 코드 checkout은 main 472aff7로 유지했고 사용자 요청에 따라 해당 이미지로 staging만 기동했다. 문서 브랜치 병합과 실제 운영 공개는 별도다. 다른 기기에서는 아래 명령으로 원격 동기화와 현재 커밋을 다시 확인한다.

```bash
git status --short --branch
git log -5 --oneline
git remote -v
git fetch origin
git branch -vv
```

커밋되지 않은 변경은 다른 기기로 전달되지 않는다. 새 기기는 `.venv`를 복사하지 않고 환경을 다시 만든다. `.env`·`.env.home`과 모델 캐시는 Git에 포함하지 않으며 비밀값은 별도로 전달한다. 경로는 각 저장소 루트를 기준으로 사용한다.

## 개발 기기와 맥미니의 작업 분담

- 개발 기기에서는 후속 코드 수정을 별도 브랜치에서 진행한다. 다음 우선 작업은 원문 근거 수집·출처 검증과 공개 전 입력·실행 제한이다.
- 맥미니에서는 현재 배포 기준을 고정하고 환경·이미지·내부 API·Tunnel 연결·부하를 검증한다. 배포 중 개발 브랜치 최신 내용을 자동으로 pull하거나 운영 이미지를 자동 교체하지 않는다.
- 맥미니에서 수정할 필요가 생기면 로컬 변경을 보존하고 별도 브랜치로 기록한다. 배포 결과는 `docs/deployment-log.md`에 실제 커밋·이미지 ID·설정 이름·검증 결과를 남겨 개발 기기로 전달한다. 비밀값은 기록하지 않는다.
- 후속 코드가 검증되면 다음 배포 커밋을 명시해 갱신한다. 서버의 환경 조정과 코드의 동작 변경을 같은 작업으로 간주하지 않는다.

## 확인된 운영 환경

- 나정균 팀원은 맥북 에어·맥미니·맥북 프로를 오가며 개발한다.
- 배포 대상은 M4 맥미니·메모리 16GB·OrbStack이다. cloudflared, Laravel, 모니터링이 기존 Docker 컨테이너로 돌아간다. 사용자에게 직접 확인했다.
- 2026-09-10 실제 맥미니의 OrbStack·16GiB 메모리와 실행 서비스 7개를 확인했다. cloudflared 컨테이너는 `lunchpick-tunnel-cloudflared-1`이며 관리 Compose·현재 네트워크는 [배포 점검 기록](deployment-log.md)에 남겼다. API 도메인·Access 정책·Vercel 주소는 여전히 미확인이다. 이번 점검은 현지 Docker 소켓으로 했으며 SSH 경로는 확인하지 않았다.
- 현재 개발 기기의 OrbStack에서 ARM64 빌드·모델 초기화·HTTP/CORS를 검증했다. 맥미니 부하나 외부 Tunnel 연결을 검증한 것은 아니다.

## 현재 실행 흐름

```mermaid
flowchart TD
    B[브라우저: Vercel 프론트 예정] -->|POST /api/analyze| A[맥미니 FastAPI: job ID 즉시 응답]
    A --> Q[메모리 작업 관리: 영상 1건 실행]
    Q --> D[yt-dlp: 영상·오디오·메타데이터 확보]
    D --> F[프레임 8장: 얼굴 crop·분류·집계]
    F --> M[미디어 결과 먼저 전달]
    M --> T[수동 자막 사용, 없으면 faster-whisper STT]
    T --> E[LLM 1: 원문 주장·문맥 추출]
    E --> C[서버: 원문 대조·발언 위치 연결]
    C --> S[주장 최대 3건 병렬: 네이버·위키 등 검색]
    S --> V[LLM 2: 주장·문맥·게시일·근거 대조]
    V --> G[서버: 인용 검증·판정/참고자료 구분]
    G --> R[주장 카드 갱신·최종 집계]
    B -->|GET /api/jobs/:id, 2~3초 폴링| A
    R --> A
```

실제 파이프라인은 미디어 분석 후 전사·주장 검증으로 진행한다. 두 축을 병렬 실행한다고 가정하지 않는다. 위 도표는 LLM 사용 경로이며, Google Fact Check 키를 켜면 기존 전문기관 판정 분기가 먼저 실행된다. 운영 예시는 해당 제공자를 제외했다. URL 재사용은 진행 중인 동일 URL에만 적용하며 완료 결과 캐시는 없다.

## 이번 변경의 API 계약

- `result.media`는 다운로드 후부터 전달되고 전사 후 `transcript_source` 등이 추가된다. 메타데이터만 도착한 것은 부분 분석 완료가 아니다.
- `job.status`는 작업 생명주기, `job.stage`는 현재 처리 단계다. `partially_completed`인 동안에도 stage는 갱신된다. 주장 카드가 준비됐는지는 claim_verification의 존재·상태로 확인한다.
- `elapsed_sec`는 호환성을 위해 접수 후 총 경과로 유지한다. 대기는 `queue_wait_sec`, 실제 분석 경과·지연 안내는 `processing_elapsed_sec`를 사용한다.
- 개별 주장은 pending → verifying → done/failed/timed_out 순서로 알림을 보낸다. 최종 summary에도 done/pending/verifying/failed/timed_out이 남는다. 미완료 표시에 failed도 포함한다.
- `claim.context`는 발언 전문에서 대조한 문맥이다. `video_title`·`video_published_at`은 판정 해석용이며 외부 근거가 아니다. 모델 입력·출력 점검은 프롬프트 평가 문서를 따른다.

## 다음 우선순위와 공개 전 남은 확인

| 항목 | 상태와 다음 작업 |
| --- | --- |
| 원문 수집·출처 독립성 | 미구현. 검색 발췌만으로 판정하는 경로는 Accepted evidence-policy를 충족하지 않는다. 원문 확보·출처별 수집 결과·원문 인용 검증·재전송 출처 중복 제거를 먼저 구현하고 실측한다. 새 프롬프트는 부족 시 유보를 요구하므로 기존보다 판정 완료 비율이 낮아질 수 있다. |
| 전문기관 판정 분기 | 판정 문자열을 먼저 채택하는 기존 구현에는 같은 주장·시점 대조가 부족하다. 맥미니 예시는 factcheck를 제외한다. 재활성화 전에 별도로 검증한다. |
| 입력 제한 | API는 http(s)만 검사하고 길이는 다운로드 후 검사한다. 공개 Shorts 전용 입력 검증과 내부 주소 접근 차단이 필요하다. 내부 홈서버에서 임의 URL을 받는 공개 운영은 아직 준비되지 않았다. |
| 실행 상한 | executor 내부 대기가 별도로 있어 queue 크기만으로 전체 접수 상한이 강제되지 않는다. 600초도 실행 중인 다운로드·STT를 끊는 제한이 아니다. 공개 전 입장 제한·타임아웃 검증이 필요하다. |
| 부분 실패 | 개별 주장 failed가 있어도 전체 작업이 completed로 끝날 수 있다. 최종 상태와 failed 집계를 함께 검증한다. |
| 미디어 정확도 | 얼굴 없는 프레임 점수가 섞이는 문제와 분류기 오탐은 worklog에 실측돼 있다. 2편으로 일반 성능을 주장하지 않는다. 전체 AI 생성 모델은 없다. |
| 실제 영상 검수 | 고정한 기대 주장·판정·출처로 M-08을 검증해야 한다. 이번 가상 자료 11건을 실제 영상 정확도나 완결성 70% 기준으로 환산하지 않는다. |
| 배포 | 전용 Compose 파일을 준비했다. 맥미니에서 기동·부하·YouTube 접근·Tunnel·Vercel CORS 확인은 별도다. 전체 job 조회(`/api/jobs`)는 디버그 기능이므로 공개 경로 정책도 점검한다. |

각 항목은 구현 확인에서 나온 후속 작업이며 팀 제품 범위를 새로 결정한 것이 아니다. docs#7에 전달할 코드와 정책 차이는 [프롬프트 평가](prompt-evaluation.md)에 정리했다.
