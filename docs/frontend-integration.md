# Vercel 프론트 연동 인수인계

작성 기준: 2026-09-12, `fix/midpoint-hardening` 배포 준비 수정안. 상세 필드·상태·오류는 [API 계약](api-reference.md)을 기준으로 한다. 이 문서는 연결 순서와 담당자별 인수 항목을 설명한다. 실제 PR·CI·서버 반영 상태는 [인수인계](handoff.md)를 확인한다.

## 먼저 구분할 상태

| 구분 | 알고 있는 사실 | 아직 확인하지 않은 것 |
| --- | --- | --- |
| 기존 개발계 배포 | main `472aff7` 이미지 기록, 사용자 로그인 후 루트 404 보고, 미인증 `/health`의 Access 로그인 이동을 브라우저에서 확인 | 서버 이미지 재조회, 로그인 후 health·Swagger와 실영상 검증 |
| Cloudflare Access | 마지막 기록은 소유자 이메일 한 개만 허용 | 팀원 이메일 추가, 실제 FE 브라우저의 로그인·쿠키·CORS |
| 작업 복사본 | 보안·부분 실패·근거 검증·Swagger 설명을 보완한 미배포 수정안 | 변경 이미지 배포 및 실제 영상·DeepSeek/NAVER E2E |
| Vercel 프론트 | 팀장님이 제작·배포할 예정 | 정확한 개발·운영 Origin과 저장소·프레임워크 |

수정안 기본 자막 정책을 기존 `manual`로 복원했다. 사용할 업로더 등록 CC가 없으면 STT로 전환하며 자동 생성 CC와 영상 속 글자의 OCR은 포함하지 않는다. 등록 CC의 정확성·전문을 보증하지 않는다. 기획 본문의 STT 방침과 팀장님의 [수동 CC 활용 가능 의견](https://github.com/Dynamic-Juo/docs/pull/7#discussion_r3958898775)은 문서 합의로 정리하되, 이번 보안 수정에 기본 정책 전환을 섞지 않고 기존 서버 환경값을 보존한다. 상세는 docs 공유 브랜치의 [백엔드 인수인계](https://github.com/Dynamic-Juo/docs/blob/docs/midpoint-review/operations/backend-handoff.md)를 함께 확인한다.

일반 사용자 공개가 목표지만 지금은 팀 개발계다. 공개 MVP의 로그인·회원가입 없음과 현재 개발계 Access 이메일 인증을 구분한다. 공개 전 남용 방지·결과 접근·실행 제한은 별도 준비와 승인이 필요하다.

## 팀장님에게 받을 정보

연결 설정을 시작하려면 다음 두 가지가 필요하다.

1. 고정된 Vercel 개발 Origin. 예: `https://프로젝트.vercel.app` 또는 확정한 custom domain. 프로토콜·호스트·필요한 포트까지이며 경로·마지막 슬래시는 제외한다.
2. 개발계 Access에 허용할 팀원 이메일 목록.

이어 받을 정보는 프론트 저장소·프레임워크, 필요한 로컬 개발 Origin(예: `http://localhost:3000`), 운영 Origin이다. 임의 Preview 주소 전체나 `*.vercel.app`을 일괄 허용하지 않는다. Preview가 필요하면 고정 테스트 도메인 또는 승인한 개별 Origin으로 범위를 정한다.

## 팀장님에게 전달할 주소

아래는 기존 API hostname과 FastAPI 코드의 경로다. 루트 `/`에는 페이지가 없어 인증 후 404가 나올 수 있다. 미인증 `/health`의 로그인 이동만 직접 확인했으며 로그인 후 문서·스키마 내용은 재검증하지 않았다.

| 용도 | 주소 | 읽는 법 |
| --- | --- | --- |
| API 기본 주소 | `https://conan-api-dev.dotseven.cloud` | `/api`를 자동으로 붙여 둔 값이 아님 |
| Swagger UI | [개발계 /docs](https://conan-api-dev.dotseven.cloud/docs) | 경로·Schemas·요청 예시, 로그인 후 Try it out |
| OpenAPI JSON | [개발계 /openapi.json](https://conan-api-dev.dotseven.cloud/openapi.json) | 해당 서버가 실제 제공하는 기계 판독 스키마 |
| ReDoc | [개발계 /redoc](https://conan-api-dev.dotseven.cloud/redoc) | 읽기용 API 설명 |
| 생존 확인 | [개발계 /health](https://conan-api-dev.dotseven.cloud/health) | `{"status":"ok"}`; 영상·키 유효성 검증 아님 |

Swagger 화면이 있어도 이번에 보완한 상세 스키마가 보이려면 수정안이 배포되어야 한다. 현재 화면과 [API 계약](api-reference.md)의 차이는 버전을 먼저 확인한다. API 기본 주소만 공개 환경변수에 넣을 수 있다. DeepSeek·NAVER 키, Cloudflare API 토큰, Access 서비스 토큰은 `NEXT_PUBLIC_*`·`VITE_*` 등 브라우저에 전달되는 환경변수나 번들에 넣지 않는다.

## 1. Swagger에서 먼저 API를 익히기

1. 허용된 이메일로 API hostname의 Cloudflare Access 인증을 마친다.
2. 같은 브라우저에서 `/docs`를 열고 Schemas와 요청·응답 예시를 확인한다. 문서를 읽기만 할 때는 분석 요청이 발생하지 않는다.
3. 실제 분석 시험을 하기로 합의한 경우에만 POST의 Try it out → Execute를 누른다. 이것은 모의 요청이 아니라 맥미니 분석·외부 제공자 호출을 일으킬 수 있다.
4. 반환된 `job_id`를 `GET /api/jobs/{job_id}`에 넣어 상태와 부분 결과를 확인한다. `display_id`는 조회 키가 아니다.

API와 같은 Origin에서 열린 Swagger는 같은 API의 인증 쿠키로 요청한다. 성공해도 **Vercel Origin에서의 교차 출처 연동까지 검증된 것은 아니다.** Swagger의 Authorize 버튼에 DeepSeek/NAVER 키나 Access 서비스 토큰을 넣어 해결하는 구조가 아니다.

## 2. Vercel 브라우저에서 연결하기

현재 연결 방식은 사용자의 브라우저가 Vercel에서 화면·자바스크립트를 받은 뒤 개발 API에 직접 요청하는 방식이다. 프론트 서버의 HTTP 요청을 분석이 끝날 때까지 열어 두지 않는다. 먼저 분석을 접수해 `job_id`를 받고 GET으로 폴링한다. [브라우저 코드 예시](api-reference.md#브라우저-요청-최소-예시)를 참고한다.

Vercel 기본 `vercel.app`과 API의 `dotseven.cloud`는 서로 다른 사이트다. 직접 연결에는 아래 조건을 함께 맞춰야 한다.

- 사용자가 API의 Access 인증을 먼저 완료한다. fetch만으로 로그인 절차가 끝나지는 않는다.
- FE는 접수와 폴링 모두 `credentials: "include"`를 사용한다. 이 옵션이 브라우저의 제3자 쿠키 제한을 해제하지는 않는다.
- BE는 정확한 **프론트 Origin**을 CORS에 허용한다. API 자신의 주소를 등록하는 작업이 아니며, 인증 쿠키 요청에 `Access-Control-Allow-Origin: *`를 쓰지 않는다.
- JSON POST의 OPTIONS 사전 요청이 로그인으로 막히지 않도록 Cloudflare와 BE의 처리 방식을 함께 정한다. POST·GET 본 요청의 Access 보호는 유지한다.
- 로그인·허용/비허용 Origin·오류 응답·인증 만료를 실제 브라우저에서 확인한다. 리다이렉트·로그인 HTML·CORS 실패는 FastAPI JSON 오류와 구별한다.

수정안은 CORS를 통해 `Retry-After`·`X-Request-ID`를 노출하도록 보완한다. 기존 서버가 수정 전이면 헤더가 읽히지 않을 수 있으므로 오류 본문의 `request_id`와 재시도 기본 안내를 사용한다. [Cloudflare 공식 CORS 안내](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/cors/)는 설정 검토용이며 현재 정책 적용 완료를 뜻하지 않는다.

제3자 쿠키 제한 때문에 실패하면 Vercel 프론트에 승인된 `dotseven.cloud` 하위 custom domain을 연결하는 방안을 검토할 수 있다. 같은 사이트여도 서로 다른 Origin이므로 CORS는 필요하다. Vercel 서버 측 프록시/BFF는 별도 설계 선택이며 이번에 구현하거나 결정하지 않았다.

## 담당자별 할 일

| 담당 | 준비·변경 범위 | 완료 증거 |
| --- | --- | --- |
| FE | 고정 Origin 제공, API 기본 주소, 접수·폴링·상태별 화면 | API 계약의 예시로 UI 테스트 |
| BE | 적용 커밋·환경 기본값 확정, 정확한 CORS Origin 반영 | 승인 후 해당 버전 OpenAPI와 브라우저 응답 |
| Cloudflare 담당 | 승인된 팀원 이메일·Access·OPTIONS 처리 | 허용 이메일 성공, 미인증·비허용 접근 차단 |
| 공동 | 로그인·접수·부분 결과·완료·만료·실패 테스트 | 체크리스트와 적용 버전 기록 |

서버·Docker·Cloudflare 조회와 변경은 사용자 승인 후 별도 수행한다. 이 문서 작성만으로 팀원 추가·CORS 변경·배포가 실행된 것은 아니다.

## FE 인수 체크리스트

- [ ] BE 커밋·이미지와 실제 OpenAPI 기준을 기록하고 미배포 문서를 배포 완료로 읽지 않았다.
- [ ] 개발 Origin·팀원 이메일·필요한 로컬 Origin을 승인받았다.
- [ ] API Swagger 같은 Origin 시험과 Vercel 교차 출처 시험을 각각 수행했다.
- [ ] 미인증·인증 만료·쿠키 제한에서 JSON 파싱 오류만 보여주지 않고 재인증·연결 안내를 제공한다.
- [ ] `job_id`·`session_id`를 보관하고 중복 POST를 자동 반복하지 않는다.
- [ ] [종료 상태](api-reference.md#작업-상태와-폴링)에서 폴링을 중지한다. 화면 이탈은 서버 작업 취소가 아님을 안다.
- [ ] 메타데이터만 있는 결과, 진행 중 카드, 분석 불가, 부분 실패, `no_claims`를 구별한다.
- [ ] `404`·`422`·`429`와 HTTP 200 안의 job.error를 다르게 처리한다.
- [ ] 미디어 점수·STT 비율을 정확도 또는 진위 확률로 표시하지 않는다.
- [ ] 검색 발췌·미검증 자료를 검증된 원문이라고 표시하지 않는다.
- [ ] 재시작·메모리 보관 한도로 결과가 소실될 수 있음을 안내한다.
- [ ] 실제 영상 검수는 기대 주장·출처·판정과 사용할 키·버전을 고정해 별도 수행한다.

## 운영 공개는 별도 단계

운영 API hostname·프론트 Origin·익명 결과 접근·요청 제한은 아직 준비를 마치지 않았다. Access 게이트만 제거해서 공개하지 않는다. 개발계 보호를 유지하고 [중간 점검·남은 과제](handoff.md), [환경 분리·배포 절차](deployment-mac-mini.md)에 따라 이미지 승격·롤백을 별도 승인한다. 같은 맥미니에서 환경을 분리해도 호스트 자원과 장애는 공유한다.
