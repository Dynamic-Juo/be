# 배포 과정에서 막힌 이유와 재발 방지

기준일: 2026-09-15. 실제 관찰한 오류와 안내 과정의 문제를 함께 기록한다. 완료 여부는 [handoff](handoff.md), FE 작업 계약은 [frontend-integration](frontend-integration.md)을 우선한다. 아래 내용은 실제 운영 교체 완료 선언이 아니다.

## 왜 단순 이미지 교체보다 오래 걸렸는가

현재 운영은 root 사용자·메모리 결과 저장을 사용하는 472aff7이다. 새 코드는 UID 10001·완료 결과 영속 저장·배포 중 접수 차단·공개 요청 보호를 전제로 한다. 따라서 새 이미지를 받는 것 외에 캐시 권한, 결과 보존, 배포 상태, 내부망 접근, 서버 인증을 함께 전환해야 한다. Cloudflare 이메일 보호가 있다는 사실만으로 컨테이너의 다른 서비스 접근까지 격리되는 것은 아니었다.

지연의 일부는 구현·검수 및 안내의 부족에서 생겼다. 실제 gh CLI에서 충돌하는 옵션을 모의 테스트가 잡지 못했고, 캐시 권한 변경 순서도 첫 실행에서 틀렸다. 필요한 키 목록을 처음부터 묶어서 설명하지 않아 사용자가 매번 다음 키를 찾아야 했다. 이런 문제를 모두 보안상 불가피한 절차라고 설명하지 않는다.

## 실제 오류·해결 기록

| 현상 | 확인된 원인 또는 아직 모르는 점 | 처리와 재발 방지 |
| --- | --- | --- |
| 코드에 Origin을 넣었는데 `Disallowed CORS origin` | 실제 컨테이너의 환경값·이미지가 로컬 코드와 달랐다 | 코드, 서버 env, 실행 컨테이너를 별도로 확인한다. 새 공개 구조는 Vercel Function이 원본을 호출하며 CORS는 서버 인증 수단이 아니다 |
| GitHub 토큰이 두 종류 필요 | 저장소/Actions 조회와 private GHCR pull의 인증 범위가 다르다 | fine-grained Actions/Contents read와 classic read:packages를 구분했다. 권한을 넓혀 하나로 합치지 않는다 |
| Apple 암호에서 기존 웹사이트 항목과 충돌 | 같은 GitHub 로그인 항목에 별도 배포 토큰을 저장하려 했다 | 토큰 이름을 별도 계정 식별자로 사용한다. 비밀값을 채팅으로 전달하지 않는다 |
| gh 서명 검증이 시작 전에 실패 | gh 2.98.0에서 `--cert-identity`와 `--signer-workflow`는 상호 배타다 | BE PR #7에서 중복 옵션 제거. 정확한 인증서 URI·ID·SHA·run 대조는 유지. 관련 테스트 180개와 실제 서버 release/OCI 검증 통과 |
| UID 10001에서 캐시 복사 실패 | 기존 root 소유 캐시 일부는 owner만 읽을 수 있다 | 원본을 read-only로 두고 새 볼륨으로 복사한다. 원본 권한을 완화하지 않는다 |
| root+CHOWN 복사 후 하위 경로 권한 오류 | mode 700 상위 폴더 소유권을 먼저 바꾸면 capability를 제한한 root가 그 아래를 탐색하지 못한다 | 깊은 경로부터 소유권을 변경했다. 새 캐시 UID 확인 및 얼굴·Whisper·ViT 오프라인 로드 성공 |
| 캐시 재시도·정리 명령이 실행되지 않음 | 자동 승인 검토가 이미지 검증 근거와 이후 정확한 변경/삭제 승인을 요구했다 | 검증된 digest를 명시하고 사용자 승인 후 재개했다. 실패 복사본 두 개의 라벨·미사용을 확인해 정리. 전체 Docker prune은 하지 않았다 |
| 정상 Access 토큰도 403 | Python 기본 User-Agent에서 모든 요청이 403이었다. 정확한 Cloudflare 규칙 원인은 미확인 | 동일 진단 User-Agent에서 정상 200·미인증/변조 302를 비교했다. 최초의 전파 지연 추정을 확정 원인으로 쓰지 않는다. 실제 Vercel 런타임 검수는 남는다 |
| CF Access Secret과 Turnstile Secret 혼동 | 서로 다른 비밀값인데 안내에서 용도 구분이 부족했다 | 아래 표처럼 화면 이름·사용처를 함께 안내한다. 입력 스크립트는 필요한 키 이름을 표시한다 |
| 저장 후 `Connection ... closed`를 실패로 인식 | 숨김 입력 프로그램이 정상 종료되면서 SSH도 끝난 것이다 | `keys saved privately`가 성공 메시지임을 먼저 안내한다. 파일 존재·owner·0600은 서버에서 재검수한다 |
| 문서마다 완료/미완료가 달라 보임 | 과거 상태를 계속 덧붙이면서 최신 결과와 이력이 섞였다 | handoff 최상단의 최신 기록을 우선하고 FE 계약의 현재 상태도 함께 갱신한다. 과거 실패는 삭제하지 않되 해소 결과를 연결한다 |
| controller에 기존 Docker/Compose 경로를 그대로 넣을 수 없음 | OrbStack 명령은 같은 멀티콜 실행 파일을 가리키는 symlink이며 controller는 고정 일반 파일 경로를 요구한다 | 기존 공식 파일의 동일 해시 복사본을 각각 docker/docker-compose 이름으로 staging 보호 경로에 두고 실행·신뢰 경로 검수. 새 패키지 설치가 아니다 |
| Homebrew gh 경로의 신뢰 검사 거절 | 상위 디렉터리에 group/world 쓰기 권한이 있다 | 기존 Homebrew 전체 권한을 변경하지 않고 감사한 동일 gh 바이너리를 보호 경로로 복사. 고정 runtime·controller 설정 완성은 별도 |

## 키를 구분하는 표

| 화면/이름 | 용도 | 보관·사용 위치 |
| --- | --- | --- |
| GitHub `chamsae-ai-deploy-read` | CI와 릴리스 자료 읽기 | 맥미니 배포 도구 전용 |
| GitHub `chamsae-ai-image-pull` | private GHCR 이미지 읽기 | 맥미니 registry 인증 전용 |
| CF `CF-Access-Client-Id` / `CF-Access-Client-Secret` | Vercel 서버가 Cloudflare Access 통과 | Vercel 서버 환경변수, 원본 인증 검수용 서버 비밀 파일 |
| Turnstile `Site Key` | 브라우저 봇 검증 위젯 표시 | 프론트 공개 설정. 비밀값이 아님 |
| Turnstile `Secret Key` | 백엔드가 Siteverify 호출 | 백엔드 전용. CF Access Secret과 교환해 쓰지 않음 |
| `PUBLIC_GATEWAY_KEY` | 백엔드 전달 인증·작업 조회 토큰 서명 | Vercel 서버와 백엔드만 공유. 생성·배포 인수 상태를 별도 확인 |

비밀값은 표·Git·PR·로그에 적지 않는다. 사용자가 입력 완료를 알려주면 존재·권한·검증 결과를 확인하며 같은 키를 다시 입력하게 하지 않는다.

## 2026-09-15 추가 검수

- Turnstile 파일 owner·0600·공개 Site Key 일치 확인. 공식 Siteverify HTTP 200, `success=false`, `invalid-input-response`를 확인했다. 의도적으로 잘못된 응답 토큰을 쓴 부정 시험이며 정상 브라우저 토큰 승인 시험이 아니다.
- a26264e 이미지를 별도 internal 네트워크·read-only rootfs·cap_drop ALL로 실행했다. 시험 프록시를 통한 실제 Turnstile 요청은 `challenge_failed`로 거절됐고, 전달 키 누락도 거절됐다.
- 같은 컨테이너에서 맥미니 SSH, Laravel 80, Redis 6379, 공용 IP 443 직접 연결이 모두 실패했다. 시험 컨테이너·네트워크는 정리했다. 이 증거는 실제 운영망 적용·독립 커널 격리를 뜻하지 않는다.

다음 완료 조건은 운영 격리와 배포 controller 구성, 새 이미지 최초 전환, 실제 영상/외부 제공자 검수다. 정상 Turnstile·Vercel 전체 경로는 FE 담당의 통합 검수와 함께 마무리한다.
