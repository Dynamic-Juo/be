# 작업 로그

## 2026-09-15 Turnstile 검수와 배포 문제 원인 정리

- 저장 파일 권한 확인과 실제 Siteverify 부정 시험을 완료했다. 새 이미지의 별도 격리망에서도 프록시를 통한 Turnstile 검증 호출·잘못된 토큰 거절·전달 키 누락 거절이 성공했고 SSH/Laravel/Redis/직접 인터넷 연결은 차단됐다. 실제 운영 전환은 아니다.
- 문제 원인/해결/재발 방지를 deployment-troubleshooting에 정리했다. 키의 역할을 순차적으로만 안내해 혼동을 키운 점과 실제 gh 호환성·캐시 소유권 변경 순서 사전 검수 부족을 명시했다.
- controller 바이너리 감사에서 OrbStack의 멀티콜 symlink 구조와 Homebrew 상위 쓰기 권한 문제를 확인했다. 기존 Docker·Compose·gh의 동일 해시 일반 파일을 staging 보호 경로에 준비했다. 기존 설치 경로 권한은 바꾸지 않았으며 launchd·실제 controller는 활성화하지 않았다.

## 2026-09-15 Turnstile 위젯 준비

- `Chamsae AI` 관리형 위젯을 `chamsae-ai.vercel.app` 한 곳에 생성하고 별도 목록에서 존재·모드·사전 승인 없음 확인. Secret은 출력하지 않고 생성 화면을 사용자 인수용으로 보존했다. 서버 숨김 입력 helper는 Secret 한 개만 받도록 준비했다. 실제 Siteverify와 FE 적용은 아직이다.

## 2026-09-15 캐시 이관 재개 및 모델 검수

- 사용자 승인 후 깊은 경로부터 소유권을 변경해 새 캐시 이관에 성공했다. UID 10001 검수 후 실패 복사본 두 개만 삭제했고 원본은 보존했다.
- a26264e 이미지의 네트워크 없는 컨테이너에서 이관 캐시를 사용한 얼굴 검출기·Whisper small CPU/int8·ViT 분류기 로드가 모두 성공했다. 운영 전환·실영상 검수는 별도다.

## 2026-09-15 Service Auth 적용 및 배포 이관 시험

- 특정 서비스 토큰 한 개의 정책 생성·앱 저장을 확인했다. 같은 진단 User-Agent에서 정상 200, 미인증/변조 302였고 Python 기본 User-Agent는 모두 403이다. 정확한 차단 규칙은 미확인이다.
- main a26264e 이미지 서명·인증서 정책 확인 및 pull 완료. 별도 컨테이너 간 완료 결과 fixture 복원 성공, 시험 결과 볼륨 제거. 실운영 전환은 미실행이다.
- 캐시 복사는 비특권 원본 접근 실패 후 root+CHOWN으로 재시도했지만 mode 700 상위 폴더 소유권을 먼저 바꿔 실패했다. 원본은 읽기 전용이었다. 실패 부분 복사본 두 개가 남았고 수정 재시도·복사본 정리는 자동 승인 검토가 거부했다. 새 이미지 사용 근거를 확인해 첫 검토 거부는 해소했지만 두 번째 삭제/변경 범위 거부는 해소하지 못했다. 상세 대상과 남은 공개 조건은 handoff에 기록했다.

## 2026-09-15 Cloudflare 서버 인증 인수

- 사용자 입력 CF 인증 파일의 소유자·mode 600·형식을 비밀값 출력 없이 확인했다. 특정 서비스 토큰 하나만 포함하는 Service Auth 정책을 저장 직전까지 준비했다. 실행 시점 확인 전이므로 정책 적용 및 실인증 검수는 미완료다.
- BE PR #7 ARM64 CI 성공 후 정확한 head를 지정해 병합했다. 운영 이미지 교체는 아직 하지 않았다.

## 2026-09-15 맥미니 자격 증명 검수와 이미지 준비

- GitHub API/GHCR 읽기 인증 및 비밀 파일 권한을 확인했다. gh 상호 배타 옵션 충돌을 발견해 BE PR #7로 수정했고 테스트 180개가 통과했다. 수정 helper로 실제 release·image bundle 및 인증서 정책 검증이 성공했다.
- 정확한 eef15c8 digest 이미지를 다운로드하고 네트워크 없는 임시 컨테이너에서 UID 10001·API 표시명·쓰기 경로를 확인했다. 기존 8개 운영 컨테이너는 교체하지 않았다. Cloudflare 인증정보용 숨김 입력 helper만 추가 준비했으며 입력 완료와 공개 전환은 미완료다.

## 2026-09-15 호스트 설치 범위 확인

- 사용자 요청으로 brew 설치 영수증·Docker 목록·프로젝트 LaunchAgents를 조회했다. 이번 직접 설치는 gh 하나이며 Python/Node/cloudflared는 이전 기록이다. GitHub self-hosted runner와 프로젝트 자동실행 plist는 설치하지 않았다. 격리 시험 이미지 1개·설정 3개가 남는다.
- 사용자가 Cloudflare 인증정보 보관을 완료했다. GHCR 익명 접근은 401이라 이미지 pull 인증을 별도로 준비해야 한다. 비밀값은 읽거나 기록하지 않았다.

## 2026-09-15 백엔드 배포 진행

- PR #5를 성공한 정확한 head b716552 기준으로 병합했다. main eef15c8의 ARM64 테스트·이미지 게시·서명 run 34862555149가 성공했다. 서버 컨테이너 자동 교체는 아직 없다.
- 맥미니에 gh 2.98.0만 설치했다. GitHub 인증 미설정을 확인했으며 기존 개발 인증을 복사하지 않았다. 앱 격리 설정은 아직 기존 상태다.
- 사용자 진행 지시 후 Cloudflare 전용 서비스 토큰 생성 요청을 보냈다. 비밀 결과 읽기·출력은 자동 승인 검토에서 거부돼 우회하지 않고 화면을 보존했다. 발급 확인·비밀 보관과 Service Auth 연결은 미완료다.

## 2026-09-15 참새 AI 주소 연결과 공개 계약 정정

- 최종 원본 `chamsae-ai-api.dotseven.cloud`와 Cloudflare 앱 `참새 AI API`를 반영했다. 기존 주소와 두 이메일의 인증 보호를 유지하고 같은 backend 원본으로 DNS/Tunnel 경로를 추가했다. 공유 Tunnel과 다른 서비스 경로는 유지했다.
- 새 주소 미인증 health 302, OPTIONS는 원본의 400 CORS 거부를 확인했다. 새 CORS·공개 보호 코드·격리 구성은 운영 미배포다. Service Token 생성은 실행 시점 확인 전이며 생성하지 않았다.
- 공개 구조의 원본 주소는 Vercel 서버 환경변수에 쓰고, 브라우저는 같은 출처 Function을 호출하도록 계약을 명확히 했다. FE/Vercel 변경은 하지 않았다. 관련 API·OpenAPI·공개 보호 테스트 70개 통과(2 warnings); 실제 공개 연동 검증은 아니다.

## 2026-09-14 네트워크 실제 검수와 작업 범위 정정

- 운영 백엔드에서 Laravel 80·Redis 6379·맥미니 Tailscale SSH 22로 TCP 연결이 가능했다. 데이터·로그인은 시도하지 않았다. 별도 internal 네트워크에서는 세 대상과 공용 HTTPS 직접 연결이 차단됐다.
- 검증용 Squid 출구는 최종 시험에서 허용 NAVER API HTTP 400(키 없음)과 미허용 도메인·사설 IP·허용 도메인의 사설 주소 해석 403을 확인했다. 초기 리다이렉트/DNS 오류와 최종 결과를 분리해 [감사 문서](deployment-isolation-audit-2026-09-14.md)에 남겼다. 운영 적용은 하지 않았다.
- ddbfa55에서 UID/GID 10001, 캐시 경로 변경, PID 128·tmpfs 768 MiB를 반영했다. 실제 ARM64 CI 34857498782가 성공했고 번들 얼굴 모델 초기화도 성공했다. 모델 전체·실영상·운영 볼륨 권한은 별도 검증이다.
- 사용자 위임을 과도하게 해석해 로컬 FE 브랜치를 수정하고 Vercel 계정 목록을 조회했다. 사용자 지적 후 작성 변경만 회수하고 FE main clean을 확인했다. 원격 FE 커밋·push·배포와 Vercel 설정 변경은 없었다. 이 작업은 FE 구현 성과로 집계하지 않는다. 앞으로 FE/Vercel은 팀장 담당이며 BE 계약 문서까지만 진행하도록 AGENTS에 기록했다.

## 2026-09-14 문서 리뷰 정리·공개 입구 보호 준비

- 사용자 요청에 따라 docs #7 약속을 기존 추적표에 통합하고 main 충돌을 해결했다. #9·#10 병합과 #11 열린 변경을 구분했다. PR·댓글·커밋 시간순 기록 및 재사용 스킬을 docs에 추가·push했다.
- 새 프론트 주소는 chamsae-ai.vercel.app이다. 공개 운영 요구에 따라 기본 비활성 gateway 모드, Turnstile 서버 확인, SQLite 한도, 작업별 조회 토큰, Vercel Function 전달 예제를 구현했다. 기획 UI 변경을 확정한 것이 아니며 FE 계약 조정과 검수가 남는다.
- 모의 Python 731 passed, 2 skipped, 2 warnings와 Node 6 passed. 재시작·동시 요청 한도, 변조·만료 토큰, 위조 전달 정보, 고비용 옵션 거부를 확인했다. 서버 격리·공개 배포·외부 E2E 검증은 미완료다. [공개 전환 인수인계](public-access.md)에 남겼다.

## 2026-09-13 완료 결과 영속 저장 준비

- 자동 watcher `0ff843c`를 PR #4 브랜치에 통합했다. 통합 모의 722개 통과(14.76초). GitHub main `513f536`의 실제 release ZIP은 API와 다운로드 크기·digest 및 3파일 계약이 일치했다. 새 이미지 서버 실행은 아직이다.

- 사용자 요청이 제3자 승인형에서 본인 main 병합 기반 자동 배포로 변경됐다. 자동 교체가 완료 결과를 유실시키지 않도록 `b04e619`에서 terminal job snapshot을 전용 볼륨에 저장·복원한다. 진행 중 작업의 비정상 장애 재개는 구현하지 않는다.
- 단일 프로세스 lock, owner-only 파일, no-follow 부모 경로, 원자 교체와 fsync, 64 MiB snapshot 상한을 적용했다. 기존 job 보관 개수 상한을 유지하고 디스크 실패 시 접수·배포 capability를 닫는다. API 응답은 결과 저장 경로나 비밀값을 노출하지 않는다.
- 전체 모의 696개 및 마지막 보완 후 관련 5개 테스트 통과. 실제 서버 설치·컨테이너 교체·새 이미지 실영상 검증은 아직 수행하지 않았다.

## 2026-09-13 통합 및 실제 Actions 재검증

- `release/dev-integration`과 BE PR #3에 세 세션을 통합했다. 문서 충돌은 이력을 보존해 해결했고 새 CD Compose의 자막 기본값 누락을 off로 수정했다.
- 로컬 모의 회귀는 691개 통과했다. 실제 Actions run `34726142851`에서 ARM64 빌드는 성공했으나 테스트 이미지에 배포 스크립트가 없어 수집 오류 7건이 발생했다. `7ae042a`에서 테스트 전용 이미지에 scripts·deploy·workflow를 포함한 뒤 run `34726301566`에서 빌드·격리 테스트가 성공했다. 앱 이미지에 host helper를 넣은 것이 아니다.
- docs 기존 공유 브랜치 `1b0f6bf`에 리뷰 미결 추적과 통합 상태를 반영했다. 서버 변경·실영상 분석·제공자 호출·배포 활성화는 하지 않았다. 상세 남은 절차는 handoff를 따른다.

기획에서 벗어난 것, 계획에 없었지만 해야 했던 변경, 테스트에서 발견한 것을 그때그때 적는다. 다듬지 않는다 — 나중에 이 로그를 근거로 `Dynamic-Juo/docs`의 평가 문서를 정리한다.

규칙은 [AGENTS.md](../AGENTS.md#작업-기록) 참고.

## 2026-09-13 — 맥미니 공존형 보안 CI/CD 컨트롤러

- 구현은 durable admission fence `6d4310a`와 서명·승인형 host controller `4b7ce7a`로 분리해 기록했다. 문서는 별도 docs commit으로 정리한다.
- 전체 구현과 문서를 `origin/ci/secure-deployment-controller`에 push해 공유했다. PR·main merge·Actions 실행·GitHub 보호 설정·맥미니 배포는 하지 않았다.
- 범용 self-hosted GitHub runner를 앱 맥미니에 설치하는 안을 제외했다. Docker socket 권한을 가진 runner가 저장소 코드와 third-party Action을 실행하면 같은 daemon의 Conan·cloudflared·Laravel·모니터링 및 향후 프로젝트까지 한 trust boundary가 되기 때문이다. GitHub-hosted CI는 build/test/sign까지만 하고 맥미니의 사전 설치 helper가 고정 target만 교체하도록 구현했다.
- image digest, release manifest, environment-gated request를 각각 Sigstore attestation과 묶었다. Host는 이름뿐 아니라 repository/owner immutable ID, exact workflow/ref/SHA/event/run/attempt, GitHub-hosted runner 인증서 identity를 검사하고 release와 image bundle을 같은 exact CI attempt에 결합한다. Workflow-controlled predicate는 권한 근거로 쓰지 않는다. 최종 감사에서 변경 가능한 repository script가 registry·OIDC 권한 job 안에서 실행되던 provenance 공백을 발견해 packages publish와 signer를 분리하고, 두 signer와 publish에서 checkout/repository code 실행을 제거했다. Manifest/request는 host-pinned workflow 본문의 `python -I` 코드만 생성한다.
- Attestation만으로 required reviewer 승인 사실이나 workflow bytes를 증명할 수 없는 틈을 닫았다. Host config schema v4가 immutable backend CI/deployment workflow 및 environment ID, 두 reviewed workflow bytes SHA-256, reviewer User immutable ID allowlist를 고정한다. Apply는 GitHub API에서 exact backend CI attempt와 exact first-attempt deployment run, 각 source commit의 workflow bytes, 현재 environment reviewer 정책과 `prevent_self_review`, 실제 승인자와 승인 뒤 current attempt를 Docker 명령 전에 확인한다. API/network 실패는 fail-closed하며 admin bypass와 branch/CODEOWNERS 보호는 activation 수동 감사로 남겼다.
- Request가 서버 path·project·service·command를 선택하지 못하게 하고, host config에 Docker/독립 Compose/gh binary SHA-256, unix endpoint, include/extends 없는 단일 flattened Compose hash, owner-only config/state directory를 고정했다. 호출자 HOME/GitHub·Docker token/context/app 환경을 상속하지 않는다.
- State/journal exact schema를 v2로 올리고 canonical SHA-256을 두 축으로 나눴다. State+journal의 `target_fingerprint`는 repository immutable identity/environment, immutable backend CI/deployment workflow 및 environment ID, Docker endpoint, project/service, global lock/state directory, durable admission protocol 같은 명시적 migration 대상을 묶는다. Journal의 `transaction_fingerprint`는 Docker/Compose binary 경로·hash, Docker config/context, Compose 경로·trusted hash, env path 같은 해당 교체의 복구 도구를 묶는다. Enabled/timeouts/bootstrap/request 값과 recover target에 불필요한 GitHub CLI 경로/hash/config, 정상 회전 가능한 두 workflow SHA-256/reviewer allowlist는 지문에서 제외했다.
- Host 경로 검증을 공용 `secure_paths.py`로 통합했다. `/`부터 component별 `openat`/`O_DIRECTORY`/`O_NOFOLLOW`, canonical raw path, root/helper owner, non-writable ancestor, private leaf parent, regular-file hard-link와 Docker socket 권한을 검증한다. Darwin extended allow ACL은 거부하고 authority를 늘리지 않는 deny-only ACL만 허용한다. 같은 macOS 로그인 UID 전체가 신뢰된다는 잔여 위험과 전용 account/별도 daemon·VM 권고를 런북에 남겼다.
- Broad fingerprint 하나를 쓰면 Compose/hash/tool을 정상 교체한 뒤 이미 완료된 journal 때문에 영구 교착될 수 있어 복구 경계를 나눴다. Target drift는 phase와 관계없이 Docker 접근 전 fail-closed하고, non-terminal 또는 state보다 앞선 journal의 transaction drift도 변경 전 거부한다. State에 이미 반영된 terminal journal의 transaction 지문만 오래된 경우에는 현재 config로 state/Compose/image ID/health/durable accepting과, desired image가 있으면 state와의 일치까지 읽기 전용으로 증명하고 아무 mutation 없이 통과시켜, 다음 apply가 새 journal을 쓰게 했다.
- 신규 접수 drain/resume을 Harness submit과 동일 lock에 연결했다. 내부 API는 loopback+32자 이상 bearer만 허용하고 forwarded client header, 잘못된 토큰, 미설정 상태를 같은 404로 처리한다. 토큰은 repr·로그·응답에 남기지 않는다. Drain/accepting marker는 atomic file+directory fsync로 남아 프로세스 재시작에도 복원되며, 손상·symlink·잘못된 owner/mode/hardlink는 startup을 막는다. `/ready.harness.admission_protocol`은 durable store가 있으면 exact `durable-api-drain-v1`, 없으면 `unavailable`이고 controller는 missing·legacy·`unavailable`을 bootstrap/start/verify/recover에서 거부한다.
- `prepared → draining → switching → verifying → committed/rolled_back/aborted` durable journal과 atomic state/image.env를 추가했다. 새 image는 startup-drained로 검증하고 commit/state 뒤 같은 image가 durable accepting이면 변경 없이 종료한다. Exact durable draining이 확인된 경우에만 fence 없이 한 번 재생성하고, start의 image·health·protocol·accepting 검증 후에는 late settle/probe/변경을 하지 않는다. Unknown·closed·probe 오류에서는 파괴 없이 닫힌다. `/ready.status=saturated`도 durable accepting이면 정상이며 recreate하지 않는다. Replay와 CI downgrade를 거부하고, commit 전 crash는 이전 image, commit 뒤 crash는 target image로 `recover`가 수렴한다. 대상 서비스 외의 `down`·volume/network/Tunnel/DNS 명령은 없다.
- 진행 중 job drain은 해결했지만 완료 결과가 process memory에만 있는 기존 구조는 유지된다. 마지막 FE poll 전 재시작 손실을 완전히 막지는 못하므로 이 위험을 수용하거나 결과를 영속화하기 전에는 매 교체를 사용자 없는 유지보수 창에서 실행하고, 무손실 요구에는 result persistence/ack가 후속으로 필요하다.
- 실제 맥미니·Docker·실행 서비스·환경 파일은 조회/변경하지 않았다. GitHub 공개 REST API는 읽기 전용으로 조회해 main의 기존 backend CI workflow 하나와 environment 0개를 확인했고 repository·Actions·environment 설정은 변경하지 않았다. 예제는 `enabled=false`, bootstrap 미설정이다. 기존 image가 drain API 이전 버전이면 최초 migration은 별도 접수 차단과 서버 변경 승인이 필요하다.
- 현재 범위는 CI와 승인형 Continuous Delivery이며 GitHub→host 자동 호출은 연결하지 않았다. 현재 image는 container root이므로 cap drop/no-new-privileges는 적용했지만 non-root 전환은 실제 ARM64 검증이 필요한 후속 hardening이다.
- 격리 가상환경에서 전체 `PYTHONPATH=. python -m pytest -q`를 실행해 **687 passed, 2 warnings (14.07초)**를 확인했다. Fake host controller, GitHub API response 검증과 filesystem/ACL fault injection을 포함한다. Python `py_compile`, workflow/Compose YAML 및 host-config JSON 파싱, `git diff --check`도 통과했다. 실제 Actions·Sigstore bundle·GHCR·Docker/OrbStack·외부 API 검증은 아니다.

## 2026-09-13 자막 기본 미사용과 선택 옵션 유지

- 사용자가 기획 결정권자는 조정준 팀장임을 명시하고 자막 미사용을 기본으로 하되 옵션을 유지하라고 지시했다. 이전 manual 기본 유지 방침을 대체하며 기획 원본을 임의 변경한 것이 아니다.
- 독립 개발 복사본의 `fix/captions-opt-in`에서 코드·Compose·신규 환경 예제를 off로 일치시켰다. API 요청과 환경변수의 manual/any 선택은 유지한다. 기본 경로가 기존 자막을 읽지 않는지, 명시적 manual에서 CC를 사용하고 STT를 건너뛰는지, 읽기 실패 시 STT로 전환하는지 회귀 검증했다.
- 전체 333 passed, 2 warnings (7.58초). 기존 개발용 Python 3.14 가상환경의 라이브러리와 Python 네트워크 차단 fixture를 사용했다. 맥미니 서버·Docker·실영상·유료 API는 조회하거나 실행하지 않았다.
- 기획·파이프라인·CI 점검 세션에 최신 지시를 전달했다. docs 점검 세션이 확인한 [다중 얼굴 댓글](https://github.com/Dynamic-Juo/docs/pull/7#discussion_r3958648232)은 여러 명 중 한 명이라도 추출되면 되는지 묻는 질문이며, 모든 얼굴 중 하나라도 이상이면 전체 이상으로 판정하라는 지시까지 확인되지는 않았다. 해당 판정 코드 변경은 하지 않았다. 실제 서버 env 전환·배포도 미실행이다.

## 2026-09-12 — Vercel 주소 확보와 승인된 서버 조회

- 프론트 Origin은 사용자 제공 `https://kimjeonil.vercel.app`다. 브라우저에서 Conan AI 첫 화면을 확인했으며 분석 버튼을 제출하지 않았다.
- 사용자가 승인한 Docker 조회 4개만 실행했다. 컨텍스트 orbstack, Conan은 이전 `conan-be:472aff7` 이미지로 healthy·재시작 0·OOM false였다. 이미지 ID는 기존 기록과 같은 `sha256:4ecdd77473ce42a9dd0799e46aafb263358dcc34a22d3d88e04c3f44c5d6aed9`이며 관리 경로는 `/Users/dotseven/srv/ConanAi/be`다. 순간 자원은 CPU 0.21%, 메모리 55.71MiB/3GiB였고 분석 부하 측정은 아니다. 기존 다른 7개 서비스도 실행 중이었다. 첫 Docker 소켓 접근은 도구 권한 제한으로 실패해 승인 범위 그대로 권한을 올려 조회했다.
- 공개 `Dynamic-Juo/fe`를 작업 폴더에 복사해 main `8c3bb9c`를 확인했다. `src/api/realClient.ts`의 실 접수·폴링은 미구현이며 현재 배포 번들에도 같은 오류 문구가 있다. API URL 환경변수 이름은 `VITE_API_BASE_URL`이다. 프론트 코드와 원격 배포는 변경하지 않았다.
- 추가 서버 조회를 설명하고 승인받아 실행했다. 서버 HEAD `472aff7`, Compose v5.1.2, `config --quiet` 성공을 확인했다. 필터한 설정은 기존 이미지, CORS `http://localhost:3000`, 외부 네트워크 `conan-staging-ingress`와 별칭 `conan-api`, 볼륨 `conan-staging_model-cache`다. 환경 전체와 키는 출력하지 않았다. 내부 `/ready`는 ready이며 작업·진행 URL·대기열은 모두 0건이었다.
- 서버 `.env.example` 삭제 변경을 발견해 보존하고 서버 `git pull`은 하지 않는다. 새 override 파일에 고정 이미지와 Vercel CORS만 지정하고 Conan 하나를 교체하는 범위의 승인을 요청했다. 아직 파일 생성·이미지 pull·교체·Access 변경은 실행하지 않았다.
- 사용자는 프론트 구현을 조정준 팀장이 맡는다고 답했다. FE 구현·PR·배포는 진행하지 않는다. 개발계 보호를 해제하거나 서비스 토큰을 프론트에 넣는 방식도 사용하지 않는다.

## 2026-09-12 — 기존 동작을 보존한 백엔드 배포 준비

- 사용자가 백엔드 배포 반영·정리를 요청했다. docs PR #7 답글은 사용자가 다음 날 마무리하며 새 docs PR은 만들지 않는다. 서버 조회·변경은 명령별 사전 승인 원칙을 유지한다.
- fetch로 be main `472aff7`, docs main `8d07ff3`과 작업 브랜치의 원격 동기화를 확인했다. 백엔드 `fix/midpoint-hardening`과 동일한 열린 PR은 준비 시작 시점에 없었다. GitHub 인증값은 출력하지 않았다.
- 보안 점검에 섞였던 자막 기본값 off 전환을 `5ed4441`에서 기존 manual로 복원했다. 코드·Compose·신규 환경 예제를 일치시키고 명시적 off·any 설정을 유지했다. 실제 `.env.home`은 조회·수정하지 않았다. 수동 등록 CC의 사람 작성·정확성·전문 여부는 보증하지 않는다.
- 전체 모의 회귀 테스트 **329 passed, 2 warnings (7.53초)**를 확인했다. 이전과 같은 Python 3.14.3 작업 가상환경, 모의 모델·제공자와 Python DNS/TCP 차단 fixture다. 실제 영상·유료 API·Docker 검증은 아니다.
- 루트 `/` 404는 사용자의 이메일 인증 후 보고와 기존 배포 커밋의 미등록 경로가 일치했다. 브라우저에서 미인증 `/health`의 Access 로그인 이동을 확인했으며 인증 후 health·Swagger·실영상 성공은 확인하지 않았다. 올바른 확인 경로와 한계를 API·FE 문서에 반영했다.
- 검색 제공자 간 병렬화, 다중 얼굴 분석, 전체 AI 생성 탐지 제외는 이번 변경에서 구현하거나 승인 완료로 처리하지 않았다. 기존 평가·댓글의 제안과 현재 코드 상태를 구분한다. `manual` 정책 복원은 기획 원본을 새로 확정하는 작업이 아니다.
- CI는 GitHub-hosted ARM64에서 앱 빌드와 네트워크 차단 테스트를 실행하고 main의 같은 검증 이미지만 GHCR에 게시한다. 자동 서버 배포는 없으며, 실제 Actions·이미지·서버 교체 결과는 완료 후 별도 기록한다.

## 2026-09-12 — API·Swagger 인수 문서와 브랜치 공유 준비

- 이번 작업은 `fix/midpoint-hardening` 작업 복사본에서만 수행했다. 브랜치 공유용 코드·테스트 `e8176d8`, CI `729be26` 커밋을 만들었다. 후속 문서와 함께 기존 브랜치에 커밋·푸시만 공유하는 범위이며 main 미반영·미배포 상태다. 새 PR 생성·PR #7 답글·병합·이미지 게시·배포는 하지 않는다. 최종 문서 커밋·원격 반영 결과는 후속 공유 기록으로 확인한다.
- 최신 전체 회귀 테스트는 **328 passed, 2 warnings**다. 작업 복사본 가상환경에서 모의 모델·제공자 및 Python DNS/TCP 차단 fixture를 사용했다. OS 차원의 전체 네트워크 격리나 실제 영상·모델·YouTube·DeepSeek/NAVER·컨테이너 검증이 아니다. 아래 9월 11일의 314개 테스트와 실제 배포/영상 실측은 각각 당시 기록으로 유지한다.
- 공개 OpenAPI에 `/health`, `/ready`, `/api/analyze`, `/api/jobs/{job_id}`의 요청·응답 스키마와 설명·예시를 보완했다. debug 목록은 문서에서 숨기며 런타임 응답을 스키마 필터링으로 바꾸지 않는다. Swagger의 인증 쿠키 요청 옵션과 CORS의 `Retry-After`·`X-Request-ID` 노출을 추가했다. 실제 도메인의 Swagger·CORS를 재검증하지 않았다.
- [API 계약](api-reference.md)을 새로 작성하고 [FE 연동 인수인계](frontend-integration.md)를 개편했다. 요청 기본값의 env 의존, HTTP 200 접수/작업 오류, 부분·종료 상태, 404·422·429, 폴링·재시도, URL만 비교하는 진행 중 중복 처리, 결과 휘발과 근거·STT 범위를 정리했다. Swagger 같은 Origin 테스트와 Vercel의 Access·쿠키·OPTIONS 테스트는 별개이며 팀원 이메일·정확한 Vercel Origin은 아직 필요하다. 서비스 토큰과 LLM/검색 키는 프론트에 전달하지 않는다.
- 9월 11일 점검에서는 읽지 못했던 PR #7 리뷰를 추가 확인했다. [수동 CC 활용 의견](https://github.com/Dynamic-Juo/docs/pull/7#discussion_r3958898775)과 본문의 STT(`off`) 방침 사이 차이를 기록하고 자동 승인으로 간주하지 않았다. [LLM 사용 의견](https://github.com/Dynamic-Juo/docs/pull/7#discussion_r3989716198)은 당시 사용 기록이며 이번 실제 env 확인이 아니다. 기획 원본은 바꾸지 않았고 정책 변경은 별도 승인이 필요하다.
- README·인수인계·배포 문서의 최신 상태를 연결했다. 배포 환경 초기화 예제는 신규 환경에만 적용하며 기존 `.env.home`·키·`manual` 설정을 보존하도록 경고했다. 서버 조회도 명령별 사전 승인이 필요하다. 이번에 서버·Docker·Cloudflare·DNS 명령이나 실제 `.env` 조회·수정을 하지 않았으며 기존 배포는 유지했다.

---

# 2026-09-10

## 기획에서 벗어난 것

| 항목 | 확정 사양 | 현재 구현 | 상태 |
| --- | --- | --- | --- |
| T-02 자막 | "MVP 분석은 자막을 사용하지 않고 음성 인식으로 확보" | 사람이 단 자막이 있으면 사용, 없으면 STT | **팀 결정 대기.** 자막 0초 vs STT 53~89초라 M-07의 "첫 결과 1분"에 직결. PR #7에서 판단 요청함 |
| T-07 / T-11 LLM | "검토 필요" — 모델 미선정 | DeepSeek으로 주장 추출·판정 구현 | **사후 승인 필요.** evidence-policy.md가 `Accepted`로 확정되면서 요소 분해 대조·사유 분류를 요구하는데, 그건 규칙이나 NLI로 불가능해서 도입했다 |
| M-01 처리 순서 | "주장 사실성 검증을 우선한다" | 미디어 조작이 먼저, 주장 검증이 나중 | **해석 확인 필요.** 처리 순서인지 화면 배치인지. 순서를 바꾸면 미디어 결과가 STT 뒤로 밀려 첫 화면이 오래 비게 된다 |
| M-03 / T-06 전체 AI 생성 | MVP 포함 | 자가표기가 있을 때만 판정, 나머지는 `분석 불가` | 팀장이 PR #7에서 1차 배제를 제안. 문서 수정 대기 |
| M-06 캐시 | "영상 ID와 파이프라인 버전으로 캐시할 수 있다" | 구현 안 함 | 팀장이 PR #7에서 "1차는 캐시 없이"라고 함. 문서 수정 대기 |
| U-03 근거 부족 사유 | 5종 (자료 없음·관련성·시점·충돌·신뢰성) | 7종 | `timeout`(시간 내 미확인)과 `partial`(일부만 확인)을 추가했다. 앞의 것은 우리 소프트 타임아웃 때문에 실제로 발생하고, 뒤의 것은 evidence-policy.md 본문이 요구한다 |

## 추가 변경이 필요해서 바꾼 것

- **네이버 검색 API 이관 대응.** 기존 개발자센터 API가 2026-07-31에 종료되고 NAVER API HUB로 옮겨졌다. 도메인·경로·인증 헤더가 전부 바뀌어 도메인만 갈아끼워선 동작하지 않는다. 규격이 어긋나면 401만 나고 근거가 조용히 비어서 원인을 찾기 어렵기 때문에 호출 URL과 헤더를 테스트로 고정했다.
- **호스트별 요청 스로틀 + 429 재시도.** 주장 검증을 3건 병렬로 바꾸자(M-07) 같은 호스트를 동시에 때려 위키백과가 429를 돌려줬고 근거가 통째로 비었다. 병렬은 유지하되 호스트 단위로만 줄을 세운다. Wikimedia가 연락처 없는 User-Agent를 더 강하게 제한하는 것도 함께 고쳤다.
- **contextvar 전파.** 병렬 스레드로 `job_id`가 넘어가지 않아 로그가 전부 `job:-`로 남았다. 워커가 여러 개 도는 서버에서는 디버깅이 불가능해진다.
- **수량 정규식에 조·경 단위 추가.** `"42조원 규모"`가 주장 후보에서 탈락하고 있었다. 뉴스에서 가장 흔한 수치 표현이다.
- **`_http_get_json`에 헤더 인자 추가.** 네이버가 인증 헤더를 요구해서 필요했다.

## 테스트에서 발견한 것

- **판정이 실제로 나온다.** 한국어 뉴스 2분 3초 영상에서 주장 8건 중 2건이 `근거와 일치`. 인용 검증도 통과했다(발췌 문장이 실제 기사 원문에 존재). 전체 92초, 주장 검증 14.2초.
- **남은 6건의 `근거 부족`은 두 종류다.**
  - STT 오류로 검색어가 틀림: `"버틴목 대출"`(→버팀목), `"투익류면제"`(→통행료 면제), `"조소득층 알들 교통카드"`(→저소득층 알뜰). 검색어가 애초에 틀렸으니 근거가 안 나온다. 테스트 기록 T-5에서 예측한 그대로다.
  - **시점 불일치**: 영상은 2022년 추석 뉴스인데 네이버는 2026년 기사를 준다. `"42조원 명절 보증자금"`은 2022년 수치고 올해는 43.4조라 안 걸린다. 지금은 `no_source`로 나오지만 `time_mismatch`가 정확하다. **검색에 발행일 범위를 넣으면 개선될 여지가 있다.**
- **LLM 주장 추출이 규칙보다 많이 뽑는다.** 같은 영상에서 규칙 5건 → LLM 8건. 규칙이 놓친 건 수치 없는 사실 주장이었다.
- **동시성 테스트가 동시성을 강제하지 않으면 버그를 놓친다.** `copy_context()`를 스레드에 넘긴 코드가 단위 테스트를 통과하고 컨테이너에서만 터졌다. 스레드가 실제로 겹치지 않고 순차로 끝나서였다. Barrier로 강제하도록 고쳤다.
- **잘못된 판정은 하나도 없었다.** 근거 없이 단정한 사례가 없다.

---

# 2026-09-10 (오후) — FE 와이어프레임 반영

## 기획에서 벗어난 것

| 항목 | 확정 사양 | 현재 구현 | 상태 |
| --- | --- | --- | --- |
| 출처 유형 라벨 | 시안은 `정부·공공기관` | 서버는 `공식 발표`(`official`) | 팀 확인 필요. 화면 문구를 바꿀지 서버 라벨을 맞출지 |
| 분석 ID | 시안 `CN-0000-0000` | `CN-XXXX-XXXX`(job id 앞 8자) | 형식은 맞췄다. 앞 8자만 쓰므로 이론상 충돌 가능하지만 신고를 job과 잇는 용도라 충분하다고 판단했다 |

## 추가 변경이 필요해서 바꾼 것

와이어프레임과 응답을 대조해 채우지 못하는 항목을 채웠다. 아래는 시안이 요구했는데 서버에 없던 것들이다.

- **썸네일·게시일** — `media`에 없었다. 게시일은 화면 표시뿐 아니라 근거 발행 시점과 주장 시점을 비교할 때도 기준이 된다.
- **발언 텍스트 출처(`stt`/`caption`)** — 시안이 발언 위치 옆에 "음성 인식"을 표시한다. 같은 타임스탬프라도 자막에서 온 것과 음성 인식에서 온 것은 정확도가 다르다.
- **발언 위치 정확도(`exact`/`approx`)** — 시안에 "00:12 ~ 00:19"와 "약 00:28"이 함께 나온다. 핵심어 겹침으로 추정한 위치를 단정해 보이게 표시하면 안 된다.
- **처리 상태별 개수** — "완료 8 · 미완료 0 · 시간 초과 0"을 만들 수 없었다.
- **분석 ID와 시각** — M-06이 피드백 폼에 분석 ID를 첨부하도록 정했는데 32자 hex는 사람이 옮겨 적을 수 없다.
- **미디어 조작 축의 분석 범위** — 시안이 "분석한 구간·프레임 범위"를 요구한다. 정상일 때는 `detail`이 `null`이라 무엇을 봤는지 알 수 없었다.

**근거 표현을 구조적으로 바꿨다.** 판정 이유가 주장에 하나뿐이었는데 시안은 근거 카드마다 이유를 보여준다. LLM에게 근거별로 인용과 이유를 받도록 프롬프트를 바꾸고, **인용 검증을 지목한 근거에 대해서만** 하도록 좁혔다. 전에는 "아무 근거에나 있으면 통과"였는데, 그러면 A 자료의 문장을 B 자료의 근거인 것처럼 붙여도 통과해서 화면에 틀린 정보가 나간다.

**판정 근거와 참고 자료를 구분했다**(`evidence[].cited`). 근거 부족인데 자료가 붙어 있으면 판정하지 않은 것을 판정한 것처럼 보인다.

**발행처는 원문 링크의 도메인을 쓴다.** 네이버 응답에 언론사명 필드가 없다. 매핑 표를 코드에 박아두는 것보다 도메인이 낫다고 봤다 — 예쁜 이름이 필요해지면 그건 코드가 아니라 데이터다.

## 테스트에서 발견한 것

- **테스트의 가짜 미디어가 실물과 따로 놀고 있었다.** `_FakeMedia`가 필드를 손으로 흉내 낸 클래스라, 실제 `VideoMedia`에 필드를 추가하자 4건이 깨졌다. 실제 dataclass 기반으로 바꿔서 앞으로는 필드가 늘어도 기본값으로 따라오게 했다. **가짜가 실물과 어긋나면 테스트는 실물이 아닌 것을 검증하게 된다.**
- 새 프롬프트가 근거별 이유와 인용을 정상적으로 낸다. 관련 없는 자료(주기율표)는 관련성 필터가 미리 걸렀다.

## 테스트에서 발견한 것 (추가)

- **응답의 `media` 블록이 새 필드를 조용히 버리고 있었다.** `report.build()`가 키를 네 개만 골라 담는 구조라, 파이프라인이 meta에 넣어 준 썸네일·게시일·발언 출처가 전부 사라졌다. 단위 테스트는 통과했고 실제 영상으로 돌려보고서야 드러났다. 담을 항목을 목록으로 두고 그 목록의 모든 키가 응답에 나오는지 테스트로 고정했다.
- **근거의 발행일 형식이 검색 수단마다 달랐다.** 네이버는 RFC 2822(`Wed, 02 Sep 2026 07:00:00 +0900`), 위키백과는 ISO(`2026-08-12T08:58:57Z`)를 준다. 화면 표시 문제이기도 하지만 더 큰 문제는 판정이다 — evidence-policy.md가 시점 비교를 요구하는데 프롬프트에 형식이 뒤섞인 날짜가 들어가면 모델이 비교하기 어렵다.
- **네이버가 2022년 기사를 제대로 찾아준다.** 시점 불일치를 걱정했는데, 실제로는 `dailian.co.kr 2022-08-11`, `newspim.com 2022-08-12` 같은 당시 기사가 나왔다. 발행일 범위 검색(B1)의 필요성이 예상보다 낮을 수 있다 — 더 재보고 판단한다.
- **근거별 이유가 실제로 유용하다.** 같은 주장에 붙은 근거 3건이 각각 다른 이유를 냈다: "1.4배로 늘린다는 주장을 직접 뒷받침한다" / "농산물에 정부 비축분을 활용함을 언급해 **주장의 일부를** 뒷받침한다" / "20대 품목 물량이 평시 대비 1.4배임을 명시한다". 부분적으로만 뒷받침하는 근거를 모델이 구분해서 적었다.

## 시안을 보고 알게 된 것

시안 요약에 **"불일치 3"**이 있다. 지금 우리가 테스트한 뉴스 영상은 정부 발표를 그대로 전한 것이라 `근거와 불일치`가 한 건도 안 나온다. **M-08 데모 목록에 알려진 허위 정보 영상이 없으면 데모에서 불일치 배지를 한 번도 못 보여준다.** 팀장에게 전달 필요.

## 추가로 채운 확정 사양 (같은 날 저녁)

- **U-03 "검증 가능한 주장이 없으면 별도 상태"** — `claim_verification.status`에 `no_claims`를 추가했다. 전에는 `analyzed` + 문장 안내라 프런트가 "볼 게 없었다"와 "보지 못했다"를 구분할 수 없었다.
- **M-07 "세션별 활성 분석 1건"** — 세션이 다른 영상을 이미 돌리고 있으면 거절한다. 대기열 포화(`server_busy`)와 원인이 달라 `session_busy`로 코드를 분리했다. 사용자에게 "서버가 바쁩니다"가 아니라 "먼저 하던 분석이 끝나야 합니다"라고 말해야 한다. 같은 URL 재요청은 기존 job 재사용이 먼저 걸리므로 새로고침은 거절되지 않는다.
- **U-01 "최종 요약"은 새로 만들지 않았다.** 시안의 요약 카드 항목이 전부 기존 필드(두 축의 `status_label`, `summary`, `job.display_id`·`created_at_iso`·`status`)로 만들어진다. 같은 정보를 담은 블록을 하나 더 두는 것은 중복이라 판단했다. 연동 때 프런트가 부족하다고 하면 그때 추가한다.

---

# 2026-09-10 (저녁) — 프레임 점수 집계 방식 비교

조정준 팀장이 PR docs#4에서 "양 끝 값을 제외하고 평균을 내는 건 어떤지" 제안해서 실측했다.

## 추가 변경이 필요해서 바꾼 것

- **집계 방식을 설정으로 뺐다**(`DEEPCHECK_FRAME_AGGREGATION`: `blend` | `trimmed_mean`). 두 방식이 정반대 위험을 감수하는 선택이라 하나를 지금 고르기보다 둘 다 두고 재는 편이 낫다고 봤다.
- **프레임별 원점수를 리포트와 signals에 남겼다.** 집계를 튜닝하거나 결과가 이상할 때 어느 프레임 때문인지 보려면 원본이 필요하다. 사용자에게는 노출하지 않는다.

## 테스트에서 발견한 것

**실측(같은 영상 2편, 재현 확인)**

| 영상 | 프레임 점수(정렬) | 블렌드 | 절사평균 | 단순평균 |
| --- | --- | --- | --- | --- |
| 한국어 뉴스(진짜 방송) | 0.997, 0.835, 0.145, 0.134, 0.086, 0.061, 0.002, 0.002 | 56.9 판단 보류 | 21.0 징후 없음 | 28.3 |
| 영어 AI 생성 | 0.999, 0.998, 0.997×4, 0.996, 0.909, 0.061 | 92.1 조작 의심 | 98.2 조작 의심 | 86.9 |

**두 방식은 높은 프레임이 1~2장일 때만 갈린다.** 여러 장이 높으면(진짜 AI 영상) 결과가 같다.

**진짜 뉴스 영상에서 2장이 0.997·0.835로 나왔다.** 분류기 오탐이다. T-13에서 확인한 모델 카드의 "학습 데이터가 3년 전이라 개념 표류가 상당하다"는 경고가 실제로 관측된 셈이고, `뚜렷한 조작 징후 없음`을 "진짜 영상"으로 표시하면 안 되는 근거가 하나 더 늘었다(U-04).

이 표본만 보면 **절사평균이 더 낫다.** 다만 표본이 2편이고, 진짜 구간 딥페이크(한 장만 진짜로 높은 경우)를 절사평균이 놓치는지는 확인하지 못했다. **정답을 아는 영상 세트(M-08 데모 점검표) 없이는 확정할 수 없다.**

## 내가 만든 테스트 버그

높은 점수가 얼굴 crop 프레임에서 나오는지 확인하려고 짠 스크립트에서, 프레임을 crop해서 채점해야 하는데 **원본 전체 화면으로 채점하고 crop 여부만 라벨로 붙였다.** 두 컬럼이 사실 같은 값이었다. 다시 돌려서 아래 결과를 얻었다.

## 얼굴 crop이 분류기를 작동시키는 유일한 요인이다

같은 프레임을 crop해서 채점한 것과 전체 화면으로 채점한 것을 비교했다.

| | 진짜 뉴스 | AI 생성 영상 |
| --- | --- | --- |
| 전체 화면 채점 평균 | 0.054 | **0.126** |
| 얼굴 crop 채점 평균 | 0.459 | **0.869** |

**전체 화면으로 보면 AI 영상조차 0.126이다.** 진짜(0.054)와 구분이 안 된다. 프레임 단위로도 극적으로 갈린다.

```
frame_003 (AI 영상)    전체화면 0.000 → 얼굴crop 0.999   (+0.998)
frame_004 (진짜 뉴스)  전체화면 0.002 → 얼굴crop 0.997   (+0.996)
```

crop 없이는 탐지 자체가 안 된다. **그런데 오탐도 crop에서 난다** — 내 원래 가설(얼굴 못 찾은 프레임이 문제)은 틀렸다.

## 지금 결과가 그럭저럭 보이는 건 잘못 두 개가 상쇄돼서다

한국어 뉴스는 얼굴이 검출된 4장 중 **2장이 오탐**이다(50%). 그런데 얼굴을 못 찾은 나머지 4장의 낮은 점수(0.061~0.145)가 집계에 섞여 그 오탐을 희석시키고 있다.

원칙적으로 그 점수들은 빼야 맞다. 얼굴 분석을 못 한 프레임을 "정상"으로 치는 셈이고, 이건 AGENTS.md의 "분석 못 함과 분석했더니 정상을 같은 값으로 응답하지 않는다"에 어긋난다. **그런데 빼면 오탐이 더 두드러진다** — 한국어 뉴스가 45.9~67.4로 올라간다.

**진짜 문제는 집계 방식도 crop도 아니고 분류기의 오탐률이다.** 유효 프레임이 4장뿐이라 오탐 1~2장이 결과를 지배한다.

## 프레임 수를 늘려도 소용없다

| 영상 | 프레임 | 얼굴 검출 | 0.5 이상 | 블렌드 | 절사평균 |
| --- | --- | --- | --- | --- | --- |
| 한국어 뉴스(진짜) | 8장 | 4장 | 2장 (50%) | 67.4 판단 보류 | 41.8 징후 없음 |
| | 16장 | 7장 | 2장 (29%) | 56.1 판단 보류 | 17.8 징후 없음 |
| | 24장 | 11장 | 5장 (45%) | 65.7 판단 보류 | 41.4 징후 없음 |
| 영어 AI 생성 | 8장 | 8장 | 7장 | 92.1 조작 의심 | 98.2 조작 의심 |
| | 16장 | 16장 | 12장 | 86.2 조작 의심 | 80.9 조작 의심 |
| | 24장 | 24장 | 19장 | 87.3 조작 의심 | 81.4 조작 의심 |

오탐률이 50% → 29% → 45%로 안정되지 않고, **판정 등급은 전혀 바뀌지 않았다.** 시간만 더 든다. 프레임 8장을 유지한다.

## 결론: 기본값을 절사평균으로 바꿨다

절사평균이 두 영상 모두에서, 프레임 수와 무관하게 옳은 등급을 냈다. 블렌드는 진짜 뉴스를 계속 "판단 보류"로 올렸다 — 단정하지는 않으니 틀린 응답은 아니지만, 진짜 뉴스를 매번 의심하면 사용자 신뢰를 잃는다.

**팀장 제안이 맞았고 내 예측(절사평균이 진짜 신호를 버릴 것)이 틀렸다.** 실제 영상에서는 그 높은 프레임이 신호가 아니라 오탐이었다.

**확정은 아니다.** 표본이 2편이고, "딱 한 장만 진짜로 높은" 구간 딥페이크를 절사평균이 놓치는지는 검증하지 못했다. M-08 데모 점검표가 나오면 같은 방식으로 다시 재고, 되돌리려면 `DEEPCHECK_FRAME_AGGREGATION=blend` 하나면 된다.

# 2026-09-10: 프롬프트·화면 응답·맥미니 인수인계 점검

## 사용자에게 확인한 운영 환경

- 개발 기기는 맥북 에어·맥미니·맥북 프로를 오간다. 특정 사용자 홈 경로에 의존하지 않는 인수인계 문서가 필요하다.
- 배포 대상은 M4 맥미니, 메모리 16GB, OrbStack이다. cloudflared, Laravel, 모니터링이 기존 Docker 컨테이너로 실행 중이다. 프론트는 팀장이 Vercel로 배포하며 주소는 미정이다.
- API 도메인, 기존 터널의 이름·네트워크, 맥미니 접근 경로는 아직 확인하지 않았다. 현재 개발 기기의 Docker 상태를 맥미니 상태로 간주하지 않는다.

## 코드·로컬 재현에서 발견한 것

- 영상 메타데이터·발언 출처는 최종 결과에만 들어갔다. 부분 결과 이후 세부 단계가 사라지고, 최종 주장 집계에서 done/pending/verifying/failed가 소실됐다. 검증 시작 스냅샷도 없었다.
- LLM이 추출한 문맥은 판정·검색에 전달되지 않았다. 영상 게시일도 판정 입력에 없었다. 정상적인 빈 claims 배열을 추출 실패로 취급해 규칙 기반 주장을 다시 만들었다.
- 모델이 부족 판정에 잘못 인용을 붙이면 cited가 남았고, 여러 인용 중 일부만 유효해도 전체 결론을 수용했다. 판정에 사용한 자료가 응답 개수 제한으로 사라질 수 있었다.
- 기존 전체 테스트 150건은 수정 후 통과했다. 이후 회귀 테스트와 실모델 점검 결과는 아래에 추가한다.

## 추가 변경

- 두 프롬프트를 deepcheck/prompts.py로 분리하고 버전 2026-09-10.2를 부여했다. 외부 데이터의 명령을 실행하지 않도록 명시하고, 조건·부정·단위·시점·독립 출처·정정 자료를 대조하도록 보완했다.
- 원문과 대조한 문맥과 영상 게시일을 전달하고 입력을 JSON으로 구조화했다. 추출 입력의 12,000자 무통지 잘림을 없앴다. 정상적인 빈 추출은 no_claims로 이어진다.
- 프론트용 메타데이터 조기 전달, stage, 대기·실제 처리 시간 분리, 검증 시작 알림과 최종 집계 보존을 추가했다.

## 기획과 남은 차이

- 검색 결과의 제목·요약문에서 인용을 찾는 현재 구현은 evidence-policy의 원문 확인을 충족하지 않는다. 위의 과거 기록 중 '기사 원문에 존재'는 실제 전체 기사 수집을 입증하지 않는다. 현재 코드는 검색 발췌만 대조한다.
- 새 프롬프트는 검색 발췌를 원문으로 간주하지 않고, 원문·독립성 확인이 안 되면 유보하도록 요구한다. 따라서 기존 표본보다 근거 부족이 늘 수 있다. 원문 수집과 독립성 검증은 아직 구현되지 않았으며, 프롬프트 규칙만으로 이를 강제했다고 주장하지 않는다.
- 공개 배포 전에 이 차이와 수집 범위를 docs PR #7에서 조정준 팀장에게 공유해야 한다. 이번 작업에서 외부 댓글이나 메시지를 전송하지 않았다.

## 검증 결과와 인수인계 산출물

- 모의 외부 호출 기반 회귀 검사 167건 통과(기존 150 + 신규 17), 의존성 deprecation 경고 2건. 실제 영상 E2E는 이번 작업에서 실행하지 않았다.
- DeepSeek `deepseek-chat`, temperature 0, 고정 가상 사례 11건: 프롬프트 v2는 10/11, v3는 11/11. 첫 실패는 부족 판정 자체가 아니라 time_mismatch/not_direct 사유 구분이었다. 원시 결과를 docs/evaluations에 보관했다. 고정 가상 원문 사례의 성공을 실제 기사 수집이나 실제 영상 정확도로 표현하지 않는다.
- 별도 ARM64 이미지 빌드 성공. 현재 개발 기기 OrbStack의 Docker 29.4.0, Compose 5.1.2에서 CPU torch 2.14.0+cpu(CUDA 없음), MediaPipe 1.0.1 import, 얼굴 검출기 초기화, uvicorn 기동, /health, 허용 Origin의 CORS OPTIONS를 확인했다. 임시 컨테이너는 외부 네트워크·호스트 포트 없이 실행 후 삭제했다. 기존 서비스와 맥미니는 변경하지 않았다.
- compose.home.yml 정적 검증에서 conan-home 프로젝트, 호스트 포트 없음, 전용 external 네트워크, ARM64, CPU 2·메모리 3GB를 확인했다. 이 자원값은 실측 최적값이 아닌 초기 제안이다.
- 얼굴 모델 다운로드 실패를 무시하는 Docker 빌드를 실패 처리로 바꿨다. .env.home을 Git·Docker 빌드 컨텍스트에서 제외하고 비밀값 없는 예제만 추적한다.
- AGENTS.md의 시작 순서를 docs/handoff.md → prompt-evaluation.md → deployment-mac-mini.md로 연결했다. 정책과 구현 차이, 공개 전 남은 작업, 서버에서 확인할 값, 복구 절차를 기록했다. 과거 로그는 보존하고 원문 검증 표현의 한계를 후속 기록으로 정정했다.

## 원격 공유 상태

- 로컬 변경은 분석 수정과 배포·인수인계 기록으로 커밋했다. 이후 사용자 요청으로 미공유 커밋의 제목과 분리를 정정했다. 현재 브랜치는 기존 main의 16개 미공유 커밋을 포함한 `fix/prompt-handoff`이며 최신 해시는 `git log`로 확인한다.
- GitHub `Dynamic-Juo/be`로의 push는 자동 승인 검토에서 명시적 외부 공유 승인 부족을 이유로 거절됐다. 사용자에게 해당 브랜치 push 승인을 요청했고 이 기록 시점에는 미실행이다. 다른 기기에서 아직 받을 수 있다고 가정하지 않는다.
- 최종 ARM64 이미지 `418d9a01b57c`에서도 프롬프트 v3와 얼굴 검출기 초기화를 다시 확인했다. 이미지 태그는 현재 개발 기기에만 있으며 레지스트리에 업로드하지 않았다.

## 커밋 규칙 적용 정정

- 사용자가 영어 커밋 제목과 에이전트 이름이 붙은 브랜치를 지적했다. 기존의 한국어 제목 관례와 변경 목적별 분리를 제대로 적용하지 못했다.
- 미공유 커밋만 분석 수정·배포 설정·문서로 나누고 한국어 제목으로 정리했다. 배포 설정이 포함됐던 docs 커밋도 분리했다. 기존 main의 16개 커밋과 구현 코드는 변경하지 않았다.
- AGENTS.md에 한국어 요약, 에이전트 이름 없는 브랜치, docs 유형의 적용 범위를 명시했다. 원격 push는 여전히 미실행이다.

## 사용자 승인 후 원격 공유 완료

- 사용자가 push를 명시적으로 승인했다. `fix/prompt-handoff`를 `Dynamic-Juo/be`에 새 원격 브랜치로 push했고 upstream 연결을 확인했다. 앞선 승인 대기 기록은 이 항목으로 해소됐다. 원격 main 병합과 맥미니 배포는 실행하지 않았다.
- 맥미니 배포 기준 구현은 `370749f`다. 개발 기기의 후속 코드 수정과 맥미니의 환경·배포 검증을 분리하고, 서버는 배포할 커밋을 고정하도록 handoff에 기록했다.

## 2026-09-10 백엔드 감사와 실제 맥미니 배포 사전 점검

- 사용자 요청에 따라 앞선 읽기 전용 감사 결과를 docs 저장소의 별도 문서 브랜치에 정리하고, be에서는 서버 점검과 handoff를 갱신한다. 구현은 수정하지 않았다. 로컬·원격 main 472aff7과 fix/prompt-handoff의 병합을 확인해 오래된 handoff를 정정했다.
- 앞선 감사에서는 임시 환경의 단위 테스트 167건이 통과했다. 실제 queue 포화, URL 중복 제거의 옵션·세션 문제, 모델 실패·provider 장애·부분 실패의 잘못된 완료 표시를 별도 재현했다. 원문·독립 출처 검증과 전체 영상 생성 모델 누락도 확인했다. 이번 문서화에서 해당 테스트를 새로 실행한 것은 아니다.
- 실제 서버의 Docker 29.4.0, Compose 5.1.2, linux/arm64, 물리 메모리 16GiB, VM 메모리 약 7.8GiB와 기존 실행 서비스 7개를 확인했다. 1회 stats의 컨테이너 메모리 합계는 약 288MiB이며 피크나 VM 전체 사용량이 아니다.
- 기존 token 기반 cloudflared의 관리 Compose 경로와 두 네트워크만 조회했다. Conan 네트워크·이미지·환경 파일은 아직 없다. 비밀값 없는 격리 작업 복사본으로 Compose 정적 검증을 통과했고 포트 미노출·전용 네트워크·CPU 2개·메모리 3GiB를 확인했다.
- 판단과 후속 항목은 [배포 점검 기록](deployment-log.md)에 남겼다. 환경 준비 후 내부 시험 배포가 가능하나, 실제 빌드·분석 부하·Tunnel·FE 인증/CORS는 미검증이다. 기존 컨테이너·네트워크·Cloudflare 설정은 변경하지 않았다.

## 2026-09-11 staging 내부 배포

- 사용자 요청으로 맥미니 main 472aff7의 ARM64 이미지를 실제 빌드하고 conan-staging 프로젝트로 기동했다. 이미지 ID·기동 시각·실행 명령은 [배포 기록](deployment-log.md)에 남겼다. 별도 네트워크·모델 볼륨을 만들고 CPU 2개·메모리 3GiB를 적용했으며 호스트 포트는 열지 않았다.
- /health·/ready, Docker healthy, CPU torch 2.14.0+cpu, 얼굴 검출기, 실제 ViT·Whisper small 로딩을 확인했다. 모델 파일 캐시는 준비됐으나 API 프로세스의 최초 모델 로딩과 실제 영상 처리 시간·피크 메모리는 미측정이다. 기존 7개 서비스는 실행 상태를 유지했다.
- 사용자가 키를 직접 넣을 .env.home을 권한 600으로 생성했다. 값은 출력하지 않았으며 Git ignore를 확인했다. 키·도메인이 없어 실제 외부 provider·Tunnel·FE 연결은 대기한다. 개발계/운영계 분리와 GitHub-hosted ARM64 CI, 이미지 승격 배포는 제안으로 문서에 기록했고 운영계·Actions runner는 생성하지 않았다.
- 후속으로 개발계 hostname을 conan-api-dev.dotseven.cloud로 확정했다. DNS는 아직 해석되지 않고 Cloudflare 로그인·Access 허용 이메일·API 키 입력을 기다린다. 현재 staging은 healthy이며 외부 연결은 미실행이다.

### 2026-09-11 — 개발계 Access·Tunnel 연결

- 사용자 승인으로 Conan API Dev 앱과 본인 이메일 한 개만 허용하는 Conan Dev Owner Only 정책을 먼저 저장한 뒤 기존 dotseven-server Tunnel에 개발계 hostname route를 추가했다. DNS 자동 생성과 정책 저장값을 확인했다.
- 기존 cloudflared에 conan-staging-ingress를 재시작 없이 연결하고 관리 Compose에 external network를 영속화했다. Compose 정적 검증과 Tunnel 네트워크에서 내부 /health 응답을 확인했다. 토큰 포함 Compose는 Git에 복사하지 않았다. 기존 7개 컨테이너와 다른 hostname 경로는 유지됐다.
- 공개 DNS 두 곳의 응답을 확인했다. 맥미니 기본 resolver는 아직 이름 해석에 실패해 공개 DNS IP를 --resolve로 지정하고 TLS 검증을 유지했다. 미인증 /health·/api/jobs GET 및 /api/analyze POST는 모두 Access 로그인으로 HTTP 302를 반환했다.
- 본인 인증 후 응답·다른 이메일 거부·키 입력 후 실제 영상 분석·FE CORS는 미검증이다. 운영계 및 Actions는 이번에도 생성하지 않았다.

### 2026-09-11 — CI 초안과 프론트 연동 조건

- 사용자 요청으로 GitHub-hosted ARM64에서 Docker 이미지 빌드·격리 테스트 후 main 이미지를 GHCR에 게시하는 Actions 초안을 추가했다. PR에서는 이미지를 게시하지 않고 provider 키를 전달하지 않는다. 실제 Actions 실행 및 GHCR 권한은 미검증이며 main 병합과 맥미니 자동 배포는 하지 않았다.
- 개발·운영은 별도 Compose 프로젝트·배포 디렉터리·환경 파일·네트워크·모델 볼륨으로 분리하고 테스트한 이미지 digest를 운영에 승격한다. 운영 hostname과 공개 범위, 제한된 배포 인증 경로는 확정이 필요하다. 맥미니에 범용 self-hosted runner를 설치하지 않았다.
- 프론트 연동에는 정확한 개발/운영 Origin, API base URL, Access 허용 대상과 인증 쿠키·OPTIONS 검증이 필요하다. 현재 본인 이메일 전용 정책은 팀장님 접속을 허용하지 않는다. POST /api/analyze의 job_id로 GET /api/jobs/{job_id}를 폴링하며 DeepSeek·NAVER 키 및 Access 서비스 토큰은 브라우저 코드에 넣지 않는다.

### 2026-09-11 — 환경 분리 준비와 로컬 CI 검증

- 일반 사용자 공개 목표를 사용자에게 확인했다. 공개 전 입력/실행/조회 제한 검증이 남아 있어 개발계 Access를 유지하고 운영 라우트는 생성하지 않았다.
- compose.home.yml의 서비스 env_file을 CONAN_ENV_FILE로 선택하고 CONAN_NETWORK_ALIAS를 환경별로 지정할 수 있도록 변경했다. 기존 기본값을 보존했다. 비밀값 없는 작업 복사본에서 conan-staging·conan-production 각각 config --quiet를 통과했다. 실제 서버 Compose 교체와 운영 기동은 하지 않았다.
- 프론트 연동 문서에 API 접수·폴링, 오류 처리, 정확한 Origin, Access 쿠키/OPTIONS, 팀장님에게 받을 정보를 정리했다.
- 임시 ARM64 컨테이너에서 기존 배포 이미지와 현재 테스트로 167 passed, 2 warnings, exit 0을 확인했다. macOS bind mount 권한 실패로 docker cp 방식으로 재실행했다. 종료 뒤 테스트의 백그라운드 다운로드가 계속되어 닫힌 스트림 로그 오류가 발생했다. 완전히 격리된 테스트로 간주하지 않으며 fixture 종료 처리가 후속 과제다. 실제 GitHub Actions 실행·새 이미지 빌드 검증은 아니다.
- 서버 .env.home 키 세 항목은 비어 있었고 일반 curl은 DNS 해석 실패였다. API 키 값은 출력하지 않았다. 따라서 실제 provider·영상 분석 검증은 진행하지 않았다.

### 2026-09-11 — 사용자 지정 영상 개발계 실측

- 사용자 지정 YouTube cYRkZmBuDqI(140초)를 배포 컨테이너 내부 API로 접수했다. job 0dd9b0fd4ce543d5bf51d38a3490c1ee는 25.1초에 completed로 종료했다. 외부 브라우저/Access E2E가 아니다.
- 다운로드 4.24초, 프레임 추출 6.16초, 미디어 분석 8.99초, 전사 5.7초였다. 8개 프레임 중 5개에서 얼굴을 검출했고 2개 프레임 점수는 약 99%였지만 집계 위험도 25.7로 뚜렷한 조작 징후 없음이 반환됐다. 이 결과만으로 영상 진위나 모델 정확도를 주장하지 않는다.
- STT는 3단어만 반환했으나 coverage_pct는 100이었다. pipeline.py는 STT duration/영상 duration 비율을 계산하므로 인식된 발언 정확도나 내용 완전성 100%를 뜻하지 않는다. 음성/음악 내용 대조와 인식 실패 원인 검증이 필요하다. 전체 AI 생성은 unavailable, 주장은 no_claims였다.
- .env.home과 실행 컨테이너에서 DeepSeek·NAVER 키 세 개가 모두 비어 있음을 값 없이 확인했다. 따라서 LLM/검색 provider 성공은 검증하지 못했다. 완료 상태를 전체 기능 성공으로 해석하지 않는다.
- 분석 후 메모리 약 1.545GiB(피크 아님), healthy·재시작 0·OOM false였다. 기존 컨테이너도 실행 상태를 유지했다.
- 공개/Tailscale/공유기 및 설정된 IPv6 DNS 서버들은 새 API A 레코드를 반환했다. macOS dscacheutil과 일반 curl은 새 도메인에 실패하지만 기존 lunchpick·cloudflare.com은 성공했다. 로컬 부정 캐시 또는 macOS resolver 경로 문제로 좁혀졌으나 캐시 초기화 전후 비교는 하지 않아 원인을 확정하지 않았다. DNS·VPN 설정은 변경하지 않았다.

### 2026-09-11 — 기존 입력 키의 배포 반영

- 사용자는 서버 .env에 이미 provider 키를 입력했다. 배포 Compose가 별도 .env.home만 읽는 차이를 해소하기 위해 세 provider 설정만 빈 배포 항목에 반영했다. 비밀값은 출력하지 않았고 두 파일 권한을 600으로 맞췄다.
- 활성 job 0을 확인한 뒤 conan-staging API만 up --no-build로 재생성했다. 기존 다른 서비스와 DNS/Access는 변경하지 않았다. 이전 키 없는 영상 테스트는 provider 검증을 대체하지 않는다.

### 2026-09-11 — 중간 점검 작업 복사본 수정

- 사용자가 전체 점검·수정을 요청하고 work/docs·work/be에서의 조회·편집·로컬 테스트만 승인했다. 서버 조회 명령도 별도 승인이 필요하다. 이번에는 배포 디렉터리·Docker·Cloudflare·macOS DNS 명령을 실행하지 않았다. .env 키도 읽지 않았다.
- docs 원격 main aedf161을 읽고 be e79a2eb에서 fix/midpoint-hardening, docs main에서 docs/midpoint-review를 만들었다. 기획 원본은 수정하지 않고 평가/RFC에 제안만 남겼다. PR 최신 웹 댓글은 읽지 못했다.
- 입력을 YouTube canonical URL로 제한하고 다운로드 전 공개/연령/길이 filter·TLS 검증·높이 제한을 보완했다. 내부 네트워크 egress·Shorts/언어 검증과 바이트 상한은 아직 남는다.
- 무제한 executor 대기 제거, 종료 시 worker 정리, 접수/세션 동시성, 공개 job의 세션정보 제거, 목록 기본 비활성, 원시 예외/요청 ID 보호, 유효 부분 결과 보존과 결과 없는 실패를 수정했다. 하드 실행 중단과 요청별 모델 자원 한도는 아직 없다.
- 얼굴 crop의 유효 점수만 집계하고 휴리스틱을 정상 판정으로 쓰지 않도록 수정했다. 필수 축 누락·주장 실패는 부분 완료에 반영했다. 전체 AI 모델과 자가표기 정책은 변경하지 않았다.
- STT 입력 길이 비율을 정확도로 오해하지 않게 설명하고 별도 세그먼트 시간 구간 비율을 제공한다. 실제 3단어 전사 품질을 해결하거나 모델·임계값을 교체한 것은 아니다. 자막 기본값은 확정 T-02에 맞춰 off로 변경했고 API 선택 옵션은 유지했다.
- 임시 폴더 finally 정리를 성공·예외 경로에서 테스트하고 삭제 실패를 상태와 로그에 남겼다. 명시적 보존 옵션과 강제 종료 한계는 문서화했다. 배포본 잔여 파일은 조회하지 않았다.
- 근거 원문/출처·인용과 추출 실패 구분을 코드에서 강제하고 프롬프트를 2026-09-11.1로 갱신했다. 실제 원문 수집과 실모델 평가는 미실행이다. 새 프롬프트에 과거 11/11 결과를 적용하지 않는다.
- HTML 근거/영상 링크의 비 HTTP(S) 스킴을 차단했다. CI 테스트는 별도 이미지에서 network none으로 실행하도록 바꾸고 PR 테스트는 읽기 권한만, main publish는 동일 검증 이미지 artifact를 이어받도록 분리했다. 실제 CI와 Docker 명령은 실행하지 않았다.
- 테스트 환경은 작업 복사본 .venv-audit의 Python 3.14.3이다. Python DNS/TCP 접근 차단 fixture와 모의 모델/provider를 사용한다. 네이티브/프로세스 전체의 OS 네트워크 격리를 뜻하지 않는다. yt-dlp 2026.8.19의 실제 metadata callback은 이미 추출된 가상 자료로 simulate 검증했으며 실제 YouTube 요청·영상 파일 생성은 없다.
- 검색 제공자 없음·실패와 정상 빈 검색을 구분했다. 장애 후 유효 근거가 없으면 failed, 일부 검색만 실패했으면 유효 자료와 범위 제한 설명을 함께 보존한다. 검색/LLM HTTP 성공 본문은 2 MiB, 오류 본문은 4,096 bytes로 제한하고 오류 응답 본문을 노출하지 않는다.
- 작업 복사본 Compose의 자막 기본값도 off로 맞추고 정적 파일 회귀 검사를 추가했다. 실제 Compose 실행·배포 환경 파일 반영은 하지 않았다. README·파이프라인 설명도 수정 코드와 과거 실측을 구분해 정정했다.
- 일괄 주장 검증 helper의 예산 초과도 done이 아닌 timed_out과 일치하는 부족 사유·라벨로 남긴다. 외부 연결 오류의 원시 reason 로그를 제거했다.
- 최종 전체 회귀 테스트 314 passed, 2 dependency deprecation warnings(6.01초), compileall·diff-check 통과. 프롬프트 평가 목록 11건/network_calls=0과 CI YAML의 권한·네트워크 정적 검사를 확인했다. 모의 테스트 시간을 실제 영상 성능으로 해석하지 않는다. 커밋·푸시·병합·이미지 게시·배포는 하지 않았다.

## 2026-09-13 — Vercel CORS·Cloudflare OPTIONS 적용 점검

- 사용자가 설정 적용을 승인했고 팀장 Access 이메일 추가를 확인했다. 외부 개발 API의 Vercel Origin·POST·content-type preflight는 403이었다. 실제 분석은 제출하지 않았다.
- main cf550f9의 기존 CORS 코드는 명시한 Origin이면 credentials를 허용한다. Vercel Origin을 환경변수로 지정한 로컬 TestClient 검사에서 OPTIONS 200, health 200과 정확한 CORS 헤더, 미허용 Origin preflight 400을 확인했다. 전체 회귀·배포 이미지 검증은 아니다.
- 저장된 home-server 및 dev-server SSH 연결은 시간 초과였고 Cloudflare 관리 연결 도구도 없었다. 서버 환경·컨테이너·Access는 변경하지 않았다. FE 연동 문서에 적용값·대시보드 위치·설정 영향·검수 조건을 기록했다. docs/vercel-access-cors는 로컬 문서 작업이며 원격 push는 하지 않았다.

## 2026-09-13 — Cloudflare OPTIONS 웹 설정 반영

- 사용자 승인으로 Chrome Apple Events 자동화를 사용했다. Conan API Dev의 options_preflight_bypass를 false에서 true로 바꾸고 저장 후 재조회했다. 기존 CORS 입력은 비어 있었고 이메일 Allow 정책·쿠키·다른 앱·Tunnel은 그대로 유지했다.
- 외부 curl의 Vercel Origin·POST·content-type OPTIONS는 400 Disallowed CORS origin으로 백엔드에 도달했다. credentials=true·GET/POST·content-type 허용 헤더를 확인했다. 미인증 GET /health는 302로 인증 보호가 유지된다. Python urllib 403/1010은 도구 요청 차단으로 구분했다.
- dev-server SSH 재시도는 시간 초과다. 맥미니 CORS 환경값 적용·컨테이너 재생성·실제 분석·Vercel 브라우저 검수는 미완료다. 변경 전으로 복구할 때는 해당 앱의 OPTIONS 원본 전달만 끈다.

## 2026-09-13 15:05 KST — 맥미니 Vercel CORS 적용 완료

- 사용자가 기존 맥북 ed25519 공개키를 등록한 뒤 `ssh dotseven@100.105.223.60`으로 접속했다. Tailscale 경로는 연결되며 이전 home-server 내부망·dev-server Tunnel SSH 시간 초과와 구분한다. 서버 Docker는 `/usr/local/bin/docker`다.
- 실제 배포 `/Users/dotseven/srv/ConanAi/be/.env.home`과 실행 컨테이너의 CORS는 localhost:3000뿐이었다. 사용자 승인 범위에서 `DEEPCHECK_CORS_ORIGINS=http://localhost:3000,https://kimjeonil.vercel.app`로 변경했다. 렌더링된 Compose를 전후 비교해 CORS 항목만 달라짐을 검증했다.
- 진행 작업 0건을 확인했다. 기존 환경 파일과 완료 결과 1건을 서버 `/Users/dotseven/srv/ConanAi/ops-backups/cors-20260913-150512`에 디렉터리 700·파일 600으로 보관했다. 결과 백업은 JSON 보관이며 API에 자동 복원되지 않는다. 구형 API 재생성으로 이전 메모리 job 조회는 사라진다.
- 기존 `conan-staging` 프로젝트의 `deepcheck-api`만 `up -d --no-build --pull never --no-deps`로 재생성했다. 현행 서버는 자동 배포 컨트롤러 전환 전 구형 Compose임을 확인했고 이번에는 이미지 교체·helper 설치·서버 git pull을 하지 않았다. 이미지 `conan-be:472aff7`, ID `sha256:4ecdd77473ce42a9dd0799e46aafb263358dcc34a22d3d88e04c3f44c5d6aed9`를 유지했다.
- 외부 curl Vercel OPTIONS는 200 OK, Allow-Origin은 정확한 Vercel 주소, Allow-Credentials=true, Allow-Methods=GET/POST, Allow-Headers=content-type이었다. 미허용 Origin OPTIONS는 400, 미인증 GET /health는 Access 302다. 내부 health는 ok, 컨테이너 healthy, 기존 다른 7개 컨테이너도 계속 Up 상태다.
- Cloudflare OPTIONS 설정과 BE 환경 반영은 완료됐다. 실제 FE의 인증 쿠키·제3자 쿠키 제한·분석 POST/GET polling·영상/외부 제공자는 이번에 검증하지 않았다. FE는 같은 브라우저에서 API Access 인증 후 credentials: include로 접수·조회한다.
- 복구가 필요하면 보관된 환경 원본과 현재 값을 비교해 CORS 항목만 되돌리고 활성 작업·현재 배포 방식 확인 후 Conan만 반영한다. 이후 다른 변경까지 원복하지 않도록 환경 파일 전체를 무조건 덮어쓰지 않는다.

## 2026-09-13 — 서버 격리 가이드라인 작성

- 사용자가 최종 프론트 흐름 성공을 보고했고, 개인 맥미니·기존 서비스 보호를 주요 작업으로 지정했다. ISO-01~07 작업 순서와 파일·네트워크·권한·자원·복구 검수 기준을 docs/server-isolation.md에 정리했다.
- OrbStack 공식 문서에서 일반/isolated 머신 모두 공유 커널이며 독립 VM 경계가 아니라는 점을 확인했다. 독립 VM과 공유 폴더·내부망 제한을 공개 목표로 제안했으며 제품·자원값은 미선정이다.
- 로컬 Compose·Dockerfile 및 이전 실제 배포 기록을 구분했다. 이번 작업에서 서버 조회·변경·격리 시험은 하지 않았다. 문서 상대 링크·Mermaid 코드 블록·git diff 공백 검사를 확인했다. 브랜치는 docs/server-isolation이고 원격 push는 하지 않았다.

## 2026-09-13 — 맥미니 배포환경 읽기 전용 감사

- 실제 inspect·proc·config 권한·네트워크·stats·배포 코드 함수 조회를 수행했다. 결과는 deployment-isolation-audit-2026-09-13.md에 기록했다. 호스트 경로/socket 비마운트, 자원·seccomp 보호와 root·NoNewPrivs/PID 보완 필요를 구분했다.
- 서버 코드가 여전히 http(s) 입력 검사와 전체 목록 노출 경로를 가진 472aff7임을 확인했다. Access는 별도 보호이며 최신 코드 미배포를 혼동하지 않는다. 기존 서비스 기능·내부망 공격·영상 요청·변경은 하지 않았다.
- ISO-01은 부분 감사다. VM 이전을 필수로 단정하지 않고 현 구성 강화와 실제 통신 경계 검사를 먼저 제안한다. 문서 링크와 공백 검사를 수행했으며 코드 테스트나 부하 시험은 하지 않았다.

## 2026-09-13 문서 리뷰 후속 정리

- docs #7의 최신 답변과 #4·#5의 논의를 확인해 과거 평가와 최신 구현·운영 상태를 분리했다. BigKinds/네이버 혼동과 자료 시점·충돌 답변의 오해를 정정하고 미결 사항을 프로젝트 계획으로 연결했다.
- #9·#10은 열린 PR이다. 개별 재검증 API, 전체 AI 생성 자가표기 처리와 인수 기대값을 [후속 문서](review-followup-2026-09-13.md)에 기록했다. 댓글·DM·서버 변경은 실행하지 않았다.
- be cf550f9의 원문·출처 충분성 gate는 이미 구현됐으나 기본 검색 제공자가 검증된 원문을 만들지 못한다. 원문 공급 경로가 없으면 실경로 양성 판정에 도달하지 못하는 점을 다음 품질 작업으로 지정했다.
- `.venv/bin/python -m pytest tests/test_claims.py tests/test_prompt_contract.py -q`: 55 passed (0.35초). 모의 계약 검사이며 실영상 품질 검수와 구분한다.
