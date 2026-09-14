# Vercel 프론트 연동 인수인계

## 현재 전달할 주소와 프론트 담당 작업

Turnstile 공개 Site Key: `0x4AAAAAAE0lGIUVFkIN-Rsz`. Cloudflare에서 `Chamsae AI` 위젯 생성과 관리형·호스트 `chamsae-ai.vercel.app` 한 개·사전 승인 없음을 확인했다. FE는 `action=analyze`로 사용한다. Secret Key는 백엔드 서버에 안전하게 인수했고 실제 Siteverify 부정 시험을 확인했다. 정상 브라우저 토큰과 실제 Vercel 전체 경로는 별도 검수가 필요하다. Site Key만으로 공개 활성화가 완료되지는 않는다.

**2026-09-15 최신 상태: 아직 공개 연결을 켜지 않는다.** 특정 Service Token의 Cloudflare Service Auth는 적용했고 정상 토큰 health 200, 미인증·변조 토큰 302를 확인했다. 하지만 운영 백엔드는 계속 472aff7이다. 새 이미지의 서명·상태 복원 시험, 캐시 이관과 얼굴·Whisper·ViT 오프라인 로드는 성공했다. 운영 격리·Turnstile·새 이미지 전환·실영상 검수가 끝나기 전에는 `PUBLIC_GATEWAY_ENABLED=false`를 유지한다. 아래 초기 기록의 “Service Auth 미적용”은 이 최신 상태로 대체한다.

팀장님이 먼저 준비할 것은 `deploy/public-gateway`의 Function/라우팅 예제 적용, 같은 출처 `/api` 호출, Turnstile `action=analyze`, 접수 응답의 작업별 토큰 보관 및 조회 시 Bearer 전달이다. 서버 원본은 아래 주소로 정하고 비밀값은 별도 안전한 전달 후 Vercel 서버 환경변수에만 설정한다. 공개 Site Key는 위에 제공했지만 공개 활성화 확인은 아직이다. 프론트 저장소나 Vercel 설정은 이번 작업에서 변경하지 않았다.

최종 API 원본은 `https://chamsae-ai-api.dotseven.cloud`다. 2026-09-15 Cloudflare DNS·Tunnel 연결과 기존 두 이메일의 Access 보호를 적용했다. 기존 `conan-api-dev.dotseven.cloud`도 같은 앱에서 보호한다. 미인증 `/health`는 302, 새 프론트 Origin OPTIONS는 백엔드의 `400 Disallowed CORS origin`이다. 서버 이미지는 여전히 `472aff7`이며 공개 보호·새 CORS는 미배포다.

공개 전환에서 팀장님께 전달할 설정은 **Vercel 서버의 `PRIVATE_API_ORIGIN=https://chamsae-ai-api.dotseven.cloud`**다. 사용자 브라우저는 Vercel의 같은 출처 API를 호출한다. 기존 브라우저용 API base만 새 도메인으로 교체하면 일반 사용자 공개까지 완료되는 것이 아니다. Service Token·전달 키는 `VITE_*`와 브라우저 코드에 절대 넣지 않는다. Function 설치, Turnstile, 작업별 조회 토큰 처리는 [공개 계약](public-access.md#설정-인수인계)을 따른다. FE 코드·Vercel 설정은 팀장 담당이며 이번에 수정하지 않았다.

아래 직접 호출·인증 쿠키 예시는 이메일 보호를 사용하는 기존 개발 연동 방식이다. 새 원본 주소에서도 이메일 인증 상태의 개발 검수에만 해당하며, 공개 Function 계약과 구분한다. 현재 Cloudflare 앱 이름은 `참새 AI API`, 기존 이메일 정책 이름은 `Chamsae AI Team Only`다.

## 2026-09-14 새 프론트 주소와 공개 접근 준비

현재 프론트는 `https://chamsae-ai.vercel.app`이다. 아래 `kimjeonil.vercel.app` 기록은 당시 적용 이력이다. 새 Origin의 실제 서버 CORS 반영은 미완료이며, 공개 전환은 [Vercel 서버 전달 방식](public-access.md)으로 준비 중이다. Function 예제와 BE 공개 보호는 작업 브랜치에 있고 FE 설치·Cloudflare Service Auth·Turnstile·서버 배포는 아직 하지 않았다. 기존 이메일 Access 정책을 유지한다.

## 2026-09-13 15:05 KST — 맥미니 Vercel CORS 적용 완료

- 사용자가 기존 맥북 ed25519 공개키를 등록한 뒤 `ssh dotseven@100.105.223.60`으로 접속했다. Tailscale 경로는 연결되며 이전 home-server 내부망·dev-server Tunnel SSH 시간 초과와 구분한다. 서버 Docker는 `/usr/local/bin/docker`다.
- 실제 배포 `/Users/dotseven/srv/ConanAi/be/.env.home`과 실행 컨테이너의 CORS는 localhost:3000뿐이었다. 사용자 승인 범위에서 `DEEPCHECK_CORS_ORIGINS=http://localhost:3000,https://kimjeonil.vercel.app`로 변경했다. 렌더링된 Compose를 전후 비교해 CORS 항목만 달라짐을 검증했다.
- 진행 작업 0건을 확인했다. 기존 환경 파일과 완료 결과 1건을 서버 `/Users/dotseven/srv/ConanAi/ops-backups/cors-20260913-150512`에 디렉터리 700·파일 600으로 보관했다. 결과 백업은 JSON 보관이며 API에 자동 복원되지 않는다. 구형 API 재생성으로 이전 메모리 job 조회는 사라진다.
- 기존 `conan-staging` 프로젝트의 `deepcheck-api`만 `up -d --no-build --pull never --no-deps`로 재생성했다. 현행 서버는 자동 배포 컨트롤러 전환 전 구형 Compose임을 확인했고 이번에는 이미지 교체·helper 설치·서버 git pull을 하지 않았다. 이미지 `conan-be:472aff7`, ID `sha256:4ecdd77473ce42a9dd0799e46aafb263358dcc34a22d3d88e04c3f44c5d6aed9`를 유지했다.
- 외부 curl Vercel OPTIONS는 200 OK, Allow-Origin은 정확한 Vercel 주소, Allow-Credentials=true, Allow-Methods=GET/POST, Allow-Headers=content-type이었다. 미허용 Origin OPTIONS는 400, 미인증 GET /health는 Access 302다. 내부 health는 ok, 컨테이너 healthy, 기존 다른 7개 컨테이너도 계속 Up 상태다.
- Cloudflare OPTIONS 설정과 BE 환경 반영은 완료됐다. 실제 FE의 인증 쿠키·제3자 쿠키 제한·분석 POST/GET polling·영상/외부 제공자는 이번에 검증하지 않았다. FE는 같은 브라우저에서 API Access 인증 후 credentials: include로 접수·조회한다.
- 복구가 필요하면 보관된 환경 원본과 현재 값을 비교해 CORS 항목만 되돌리고 활성 작업·현재 배포 방식 확인 후 Conan만 반영한다. 이후 다른 변경까지 원복하지 않도록 환경 파일 전체를 무조건 덮어쓰지 않는다.

## 2026-09-13 Cloudflare OPTIONS 반영 완료

사용자가 Chrome 자동화를 허용한 후 Cloudflare 웹에서 Conan API Dev의 `옵션 요청을 원본으로 바이패스`를 켜고 저장했다. 설정을 다시 열어 `options_preflight_bypass=true`를 확인했다. 기존 Cloudflare CORS 입력은 비어 있었으며 이메일 Allow 정책·쿠키 설정·다른 앱·Tunnel은 변경하지 않았다.

- 외부 curl의 Vercel preflight는 이제 **HTTP 400, Disallowed CORS origin**이다. Allow-Credentials=true, Allow-Methods=GET/POST, Allow-Headers=content-type이 반환되어 OPTIONS가 백엔드 CORS까지 도달함을 확인했다. Vercel Origin 허용은 아직 완료되지 않았다.
- 쿠키 없는 GET /health는 **HTTP 302**로 계속 인증을 요구한다. 실제 분석 POST·인증 후 브라우저 연동은 실행하지 않았다.
- Python urllib 검사는 Cloudflare 1010 차단으로 판정에 사용하지 않고 curl 응답을 기준으로 삼았다.
- Cloudflare 로그인 후에도 dev-server SSH는 시간 초과다. 서버 환경·컨테이너는 미변경이며 아래의 CORS 환경값 반영이 남았다. 아래 초기 관리 도구 부재 기록은 Chrome 자동화로 해소됐지만 맥미니 접속 문제는 남아 있다.

## 2026-09-13 CORS·Access 적용 작업 상태

사용자는 백엔드 CORS와 Cloudflare OPTIONS 설정 적용을 승인했고, 팀장 이메일을 Access에 추가했다고 확인했다. 아래의 과거 '팀원 이메일 미확인' 기록보다 이 확인을 우선한다. 실제 허용 목록은 이번에 조회하지 않았다.

- 개발 API에 Vercel Origin·POST·content-type을 지정한 외부 OPTIONS 요청은 HTTP 403이었다. 분석 POST는 제출하지 않았다.
- main `cf550f9` 코드는 명시한 Origin에 대해 인증 쿠키를 허용한다. 로컬 TestClient에서 `DEEPCHECK_CORS_ORIGINS=https://kimjeonil.vercel.app`로 OPTIONS 200, GET /health 200, 정확한 Allow-Origin·Allow-Credentials 응답과 미허용 Origin의 preflight 400을 확인했다. 서버 이미지의 동작을 검증한 것은 아니다.
- 기존 SSH 별칭 home-server와 dev-server는 모두 시간 초과였다. 이 세션에는 Cloudflare 관리 연결 도구도 없어 서버 환경·컨테이너·Access 설정을 변경하지 못했다. 적용 완료나 Vercel 연동 성공으로 읽지 않는다.

연결 가능한 맥미니 작업에서 실제 CORS 값과 배포 방식을 먼저 확인한다. 기존 허용 Origin은 보존하면서 `https://kimjeonil.vercel.app`을 추가하고 `*`는 사용하지 않는다. 마지막 확인값이 localhost:3000뿐인 경우 목표 값은 다음과 같다.

```dotenv
DEEPCHECK_CORS_ORIGINS=http://localhost:3000,https://kimjeonil.vercel.app
```

환경 파일 전체를 예제로 덮어쓰지 않는다. 환경값 적용에는 실행 컨테이너 재생성이 필요하므로 현재 작업·배포 컨트롤러 상태를 확인하고 현행 배포 절차로 반영한다. 이번 요청을 다른 이미지·자막 정책·자동 배포 설치까지 승인한 것으로 확대하지 않는다.

Cloudflare는 **Zero Trust → Access controls → Applications → 참새 AI API → Configure → Advanced settings → Cross-Origin Resource Sharing (CORS) settings**에서 **Bypass OPTIONS requests to origin**을 켠다. 먼저 백엔드 CORS 적용을 확인한다. 이 설정은 해당 앱의 기존 Cloudflare CORS 설정을 제거하므로 변경 전 값을 기록하고 다른 설정이 있다면 영향 범위를 대조한다. 이메일 Allow 정책과 실제 POST·GET의 Access 인증은 유지한다. [공식 설정 설명](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/cors/#bypass-options-requests-to-origin)

적용 후 외부 OPTIONS가 성공하고 정확한 Vercel Origin·credentials·POST·content-type을 허용하는지 확인한다. 미인증 GET /health는 계속 Access에 차단되어야 한다. 이후 허용 이메일로 로그인한 브라우저에서 FE의 credentials: include와 함께 접수·폴링을 별도 검수한다. 서버 설정 검사만으로 제3자 쿠키 제한이나 FE 연동까지 성공했다고 기록하지 않는다.

작성 기준: 2026-09-12, `fix/midpoint-hardening` 배포 준비 수정안. 상세 필드·상태·오류는 [API 계약](api-reference.md)을 기준으로 한다. 이 문서는 연결 순서와 담당자별 인수 항목을 설명한다. 실제 PR·CI·서버 반영 상태는 [인수인계](handoff.md)를 확인한다.

## 먼저 구분할 상태

| 구분 | 알고 있는 사실 | 아직 확인하지 않은 것 |
| --- | --- | --- |
| 기존 개발계 배포 | 승인된 서버 조회에서 `conan-be:472aff7` healthy, 내부 `/ready`의 진행·대기 작업 0건 확인 | 새 이미지 교체, 로그인 후 health·Swagger와 실영상 검증 |
| Cloudflare Access | 마지막 기록은 소유자 이메일 한 개만 허용 | 팀원 이메일 추가, 실제 FE 브라우저의 로그인·쿠키·CORS |
| 작업 복사본 | 보안·부분 실패·근거 검증·Swagger 설명을 보완한 미배포 수정안 | 변경 이미지 배포 및 실제 영상·DeepSeek/NAVER E2E |
| Vercel 프론트 | `https://kimjeonil.vercel.app`, `Dynamic-Juo/fe` main `8c3bb9c`, React·Vite. 실 API 클라이언트는 미구현 | 조정준 팀장의 클라이언트 구현과 실제 연동 검수 |

서버의 현재 CORS는 `http://localhost:3000`만 허용한다. Vercel Origin 추가와 새 이미지 교체는 제안한 상태이며 아직 실행하지 않았다. 프론트 코드·PR·Vercel 배포는 조정준 팀장이 담당한다.

프론트에 전달할 공개 설정은 다음과 같다. Vite 빌드에 반영한 뒤 팀장님이 재배포해야 한다. 환경변수만 지정해도 미구현 클라이언트가 완성되는 것은 아니다.

```dotenv
VITE_API_BASE_URL=https://conan-api-dev.dotseven.cloud
VITE_USE_MOCK=false
```

팀장님은 `src/api/realClient.ts`에 `POST /api/analyze` 접수와 `GET /api/jobs/{job_id}` 폴링을 구현한다. 인증 쿠키 전송, 인증 실패·비 JSON 응답 구분, 종료 상태·404의 폴링 중단, 429의 `Retry-After` 처리는 아래 절차와 API 계약을 따른다. 분석 POST를 자동 재시도하지 않는다.

2026-09-13 사용자 지시에 따라 기획 결정권자는 조정준 팀장이며 자막 기본값은 `off`(미사용·STT)로 맞춘다. `manual`은 등록 수동 CC 우선, `any`는 자동 CC도 허용하는 선택 옵션으로 유지한다. 사용할 CC가 없으면 STT로 전환하며 영상 속 글자의 OCR은 포함하지 않는다. 등록 CC의 정확성·전문을 보증하지 않는다. 과거 manual 기본 유지 기록은 이번 지시로 대체됐다. 이 문서는 작업 브랜치 기준이며 실제 서버 환경값과 이미지는 바꾸지 않았다. FE에서 자막 미사용을 명시하려면 요청에 `caption_policy: "off"`를 넣는다. 진행 중 동일 URL은 옵션이 달라도 먼저 접수된 작업을 재사용하므로 옵션 비교는 기존 작업 종료 후 수행한다.

일반 사용자 공개가 목표지만 지금은 팀 개발계다. 공개 MVP의 로그인·회원가입 없음과 현재 개발계 Access 이메일 인증을 구분한다. 공개 전 남용 방지·결과 접근·실행 제한은 별도 준비와 승인이 필요하다.

## 팀장님에게 받을 정보

확인된 주소와 남은 확인 사항은 다음과 같다.

1. 고정 Vercel Origin은 사용자 제공 `https://kimjeonil.vercel.app`다. 경로·마지막 슬래시는 제외한다.
2. 개발계 Access에 허용할 팀원 이메일 목록은 아직 필요하다.

추가 로컬 개발 Origin과 향후 운영 Origin은 별도로 확인한다. 임의 Preview 주소 전체나 `*.vercel.app`을 일괄 허용하지 않는다. Preview가 필요하면 고정 테스트 도메인 또는 승인한 개별 Origin으로 범위를 정한다.

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
