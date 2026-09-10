# 프론트 연동 인수인계

## 현재 상태

개발 API는 `https://conan-api-dev.dotseven.cloud`다. Cloudflare Access는 현재 소유자 이메일 한 개만 허용한다. 프론트 개발자는 별도 허용이 필요하다. 운영은 일반 사용자 공개가 목표지만 아직 공개하지 않았다. 운영 API hostname, 프론트 개발·운영 Origin, 회원/비회원 인증 방식은 미확정이다.

프론트 배포 서버가 분석을 기다리는 구조가 아니다. 사용자의 브라우저가 API에 분석을 접수하고 같은 API에서 결과를 조회한다. DeepSeek·NAVER 키와 Access 서비스 토큰은 프론트 환경변수나 번들에 넣지 않는다.

## 팀장님에게 받을 정보

- 고정된 개발 프론트 Origin과 운영 Origin. 예: 프로토콜과 hostname만 포함하며 경로나 마지막 슬래시는 제외한다. 임의 Preview 도메인 전체를 허용하지 않는다.
- 개발계 Access에 허용할 팀원 이메일.
- 공개 서비스에서 회원 인증을 사용할지 여부. 현행 session_id는 인증 수단이 아니다.

## API 사용 순서

1. 개발계에서는 API hostname에 먼저 직접 접속해 Access 인증을 완료한다.
2. POST `/api/analyze`에 `{"url":"분석할 영상 URL"}`을 전송한다. 응답의 `job_id`, `session_id`를 보관한다.
3. GET `/api/jobs/{job_id}`를 2~3초 간격으로 조회한다. 실행 중 동일 세션에서 추가 분석을 접수하지 않는다.
4. job.status와 job.stage, 부분 결과를 구분해 표시한다. 개별 주장 실패가 있는 completed 결과도 있으므로 상태 하나만으로 모든 분석 성공을 표시하지 않는다.
5. 429는 요청 과다/세션 충돌의 error.code를 보고 안내한다. 404는 서버 재시작 등으로 작업이 소실될 수 있으므로 재접수 여부를 사용자에게 묻는다.

요청 코드의 기본 형태:

```javascript
const apiBase = "https://conan-api-dev.dotseven.cloud";
const response = await fetch(`${apiBase}/api/analyze`, {
  method: "POST",
  credentials: "include", // Access 보호 개발계의 인증 쿠키
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ url: videoUrl }),
});
// response.ok, Content-Type, 오류 본문을 확인한 뒤 job_id를 사용한다.
```

로그인 화면으로 리다이렉트된 응답은 API JSON이 아니다. fetch만으로 Access 로그인을 끝낼 수 있다고 가정하지 않는다. 서로 다른 사이트의 쿠키 정책 때문에 Vercel 기본 도메인에서 제약이 생길 수 있어, 실제 프론트 주소에서 검증하고 필요하면 같은 상위 도메인의 프론트 custom domain을 팀과 정한다.

## 백엔드·Cloudflare에서 맞출 것

- `DEEPCHECK_CORS_ORIGINS`에 전달받은 정확한 프론트 Origin을 등록한다. 프론트 Origin 대신 API 주소를 등록하지 않는다. CORS는 사용자 인증이 아니다.
- 개발계 Access 허용 이메일과 쿠키, OPTIONS preflight 응답을 함께 확인한다. POST/GET 본 요청에 대한 Access 보호는 유지한다.
- 허용 Origin과 비허용 Origin을 각각 테스트하고 실제 브라우저에서 접수·폴링까지 확인한다. 내부 API CORS 통과만으로 Cloudflare 경유 성공을 주장하지 않는다.
- 참고: [Cloudflare Access CORS](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/cors/).

## 공개 전 필수 조건

임의 URL/내부 주소 접근 차단, 다운로드·분석 실행 제한, 전체 접수 상한과 요청 제한, 작업 조회 권한 및 디버그 목록 공개 여부를 검증한다. 운영의 이메일 게이트 제거만으로 안전한 공개 서비스가 되지 않는다. 현재 Access 정책은 그대로 유지한다.

## 개발·운영 분리 준비

`compose.home.yml`은 `CONAN_ENV_FILE`로 서비스 환경 파일을 선택할 수 있다. 기본값 `.env.home`은 기존 배포와 호환된다. `CONAN_NETWORK_ALIAS`도 환경별로 지정할 수 있다. Compose 프로젝트·배포 디렉터리·네트워크·환경 파일을 각각 분리하고 프로젝트별 model-cache 볼륨을 사용한다.

`--env-file`은 Compose 치환용이고 `CONAN_ENV_FILE`은 컨테이너에 주입할 파일 선택용이다. 두 파일을 다르게 선택해 개발 키가 운영에 들어가지 않도록 같은 환경을 가리키게 한다. 상대 경로 기준이 헷갈리지 않도록 각 배포 디렉터리 안에서 실행한다.

운영 생성과 자동 배포는 아직 하지 않았다. CI의 검증된 이미지 digest를 개발에서 시험한 뒤 운영으로 승격한다. main 병합·GHCR 게시 권한·맥미니의 제한된 이미지 pull/배포 권한·이전 digest 롤백 검증이 남아 있다. 같은 호스트의 자원 경합과 장애는 환경 분리로 해결되지 않는다.
