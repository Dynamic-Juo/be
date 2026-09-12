# 개발계 승인형 CD 런북

- 상태: `Draft`
- 기준일: 2026-09-13 KST
- 코드 기준: be 원격 main `38bd16263afbf79daffd67878277c3af0a4a5d51`
- 준비 브랜치: `ci/dev-deployment`
- 대상: 개발계 한 곳. 운영계는 범위 밖이다.

이 문서는 테스트한 ARM64 이미지를 digest로 고정하고 승인 요청과 서버 교체를 분리하는 절차다. 현재 구현은 GitHub-hosted runner에서 승인 요청 artifact를 만드는 지점까지만 연결한다. 맥미니 runner, SSH, secret, environment, 서버 설정과 실제 배포는 만들거나 실행하지 않았다.

## 현재 확인된 상태

| 항목 | 확인 결과 | 근거와 한계 |
| --- | --- | --- |
| 원격 main | `38bd16263afbf79daffd67878277c3af0a4a5d51` | 2026-09-13 `git fetch origin`과 공개 GitHub API 결과가 같다. |
| 기존 CI | ARM64 앱 이미지 빌드, 테스트용 파생 이미지의 네트워크 차단 테스트, 동일 앱 이미지 GHCR 게시로 나뉜다. | [기존 workflow](../.github/workflows/backend-ci.yml)와 [Actions run 34625602926](https://github.com/Dynamic-Juo/be/actions/runs/34625602926)을 확인했다. |
| Actions 결과 | main push run의 `test-build`와 `publish` job 및 주요 단계가 모두 `success`다. | GitHub API에서 2026-09-13 재확인했다. 실제 영상·외부 제공자·서버 검증은 아니다. |
| 게시 digest | 이전 게시 로그 기록은 `ghcr.io/dynamic-juo/be@sha256:c6e1091c982e528140b06a708573756f6598ace6f43eed6a6b83bf5b5419bec0`이다. | Actions publish 성공은 재확인했다. 익명 GHCR package API는 401이어서 이 세션에서 registry manifest와 pull 가능 여부를 독립 확인하지 못했다. |
| 기존 개발계 | 과거 승인 점검 기록은 서버가 `conan-be:472aff7`을 사용했고 `38bd162` 이미지는 미배포라고 한다. | 이번 작업에는 서버 조회 승인이 없어 현재 상태로 재확인하지 않았다. |
| 자막 정책 | main의 코드·개발 Compose 기본은 `manual`이다. `origin/fix/captions-opt-in`의 코드 커밋 `58a51cf`는 기본을 `off`로 바꾸고 옵션을 유지하며, 브랜치 최신은 `1b58097`이다. | `off` 변경 브랜치는 main 미병합·서버 미배포다. 해당 세션 기록의 333개 모의 테스트 성공은 이 CD 세션에서 재실행하지 않았다. CD는 자막 환경값을 생성·출력·수정하지 않는다. |

`sha-<commit>` 태그는 배포 식별에 편리하지만 불변값은 아니다. 같은 source SHA의 workflow를 다시 실행하면 떠 있는 base image, apt와 Python 의존성 때문에 다른 이미지가 만들어지고 같은 태그를 다시 가리킬 수 있다. 배포 기준은 성공한 main push의 `run_id`·`run_attempt`와 실제 `repo@sha256:<digest>`의 조합이다.

## 준비한 흐름

```mermaid
flowchart LR
    M[main push] --> C[기존 ARM64 build와 격리 test]
    C --> P[테스트한 동일 image 게시]
    P --> D[CI run과 digest metadata]
    D --> V[수동 CD 요청: run과 metadata 검증]
    V --> A[conan-development 승인]
    A --> R[environment-bound request artifact]
    R -. 접근 방식 미정 .-> H[호스트에 사전 설치한 신뢰 helper]
    H --> I[외부 접수 차단과 idle 확인]
    I --> S[대상 서비스만 교체]
    S --> Q[/health, /ready, image ID 확인]
    Q -->|성공| K[rollback image 보존]
    Q -->|실패| B[이전 image로 자동 복구]
```

준비 브랜치의 [backend-ci.yml](../.github/workflows/backend-ci.yml)은 앱 이미지를 다시 빌드하지 않는 기존 publish 구조를 유지한다. 게시와 release metadata 생성은 `push/main`에만 허용하고, 수동 dispatch는 build/test까지만 수행한다. 게시 후 실제 RepoDigest, source SHA와 CI run 정보를 `conan-release-<sha>-<attempt>` artifact로 30일간 남긴다. 새 이미지에는 `org.opencontainers.image.revision`과 `org.opencontainers.image.source` 라벨을 넣는다. 선행 job이 만든 artifact 이름을 job output으로 소비 job에 전달해 `Re-run failed jobs`에서도 현재 attempt를 잘못 추정하지 않는다. 이 동작은 실제 Actions에서 아직 실행 검증하지 않았다.

기존 run `34625602926`은 이 metadata와 revision 라벨을 만들기 전 실행이다. 기록된 digest가 있더라도 새 검증·승인 흐름의 입력 계약을 충족하지 않는다. 준비 변경이 main에 검토·병합된 뒤 발생한 새 `push/main` CI run부터 이 흐름을 사용할 수 있다. 현재 run을 새 방식으로 배포 완료 처리하지 않는다.

[dev-deployment.yml](../.github/workflows/dev-deployment.yml)은 `workflow_dispatch`만 허용한다. 입력한 run이 이 저장소의 성공한 `push/main` backend CI이고 SHA·attempt가 일치하는지 GitHub API와 metadata로 확인한다. dispatch 시점의 `github.sha`를 두 job 모두 exact checkout하므로 environment 승인 대기 중 main이 전진해도 다른 스크립트를 실행하지 않는다. `validate`는 검증 artifact만 만들고, `approve`는 아래 두 활성화 표식과 `conan-development` environment를 참조하는 environment-bound request artifact를 만든다.

- 저장소 변수 `CONAN_DEV_DEPLOY_ENABLED=true`
- environment 변수 `CONAN_DEV_ENVIRONMENT_CONFIGURED=true`

두 변수는 현재 만들지 않았다. `vars` context는 값이 environment·repository·organization 중 어느 scope에서 왔는지 증명하지 않으므로 표식 자체가 required reviewer·prevent self-review·main-only branch rule의 기계적 증거는 아니다. 같은 이름의 repository/organization 변수는 두지 않고, 저장소 관리자가 규칙을 API 또는 설정 화면에서 직접 확인한 뒤 environment 변수와 저장소 활성화 변수를 설정해야 한다. Request의 `dispatch_actor`는 workflow를 시작한 사람이지 environment 승인자가 아니다.

이 workflow에는 서버 주소, Compose 경로, SSH, self-hosted runner와 Docker 명령이 없다. Environment-bound artifact 자체는 승인자의 신원이나 보호 규칙 구성을 증명하지 않으며 실제 배포를 의미하지 않는다. 소비자는 exact run ID·attempt가 성공했고 `conan-development` 보호를 통과한 artifact인지 GitHub 측 기록과 함께 확인해야 한다. 이 검증 연결은 활성화 전 미완료 항목이다.

## GitHub 보안 기준

| 통제 | 적용 또는 결정 기준 |
| --- | --- |
| 최소 권한 | workflow 기본 권한은 `{}`다. 검증 job만 `contents: read`, `actions: read`를 사용하고 기존 이미지 publish job만 `packages: write`를 유지한다. [workflow permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions)을 따른다. |
| Action 고정 | 모든 `uses:`는 full commit SHA로 고정한다. GitHub는 full-length SHA 고정을 변경 불가능한 action 사용 기준으로 안내한다. [Secure use reference](https://docs.github.com/en/actions/reference/security/secure-use#using-third-party-actions)를 따른다. |
| 승인 | `conan-development`에 required reviewer를 지정하고 prevent self-review를 켠다. 관리자 우회를 끄고 deployment branch를 `main`으로만 제한한다. Required reviewer는 여러 명을 넣어도 한 명의 승인으로 진행된다는 점을 승인자 구성에 반영한다. [Deployments and environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)와 [환경 관리](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)를 따른다. |
| 동시 실행 | 고정 concurrency group `conan-development-deployment`와 `cancel-in-progress: false`를 사용한다. 새 요청이 실행 중 교체를 취소하지 않는다. 기본 대기열은 새 pending 요청이 기존 pending 요청을 대체할 수 있으므로 승인된 모든 요청 보존이 필요하면 별도 정책을 결정한다. [concurrency](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#concurrency)를 따른다. |
| 불변 이미지 | 태그를 다시 해석하지 않고 GHCR `@sha256:` digest만 허용한다. [GHCR pull by digest](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#pull-by-digest)를 따른다. |
| self-hosted runner | 앱 서버에 범용 persistent runner를 설치하지 않는다. GitHub는 self-hosted runner가 매 job마다 깨끗한 VM을 보장하지 않으며 공개 저장소의 신뢰하지 못한 코드에 특히 위험하다고 경고한다. [self-hosted runner hardening](https://docs.github.com/en/actions/reference/security/secure-use#hardening-for-self-hosted-runners)을 따른다. |
| provenance | Artifact attestation은 생성 후 배포 시 검증해야 의미가 있다. 저장소 plan과 사용 가능 여부, 검증 주체를 확인한 뒤 추가한다. 현재 구현 범위에는 넣지 않았다. [artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)을 따른다. |

현재 저장소는 공개 상태로 GitHub API에서 확인했다. Required reviewer와 attestation의 실제 조직 권한·plan 지원 여부는 설정 전에 확인한다. Environment 승인은 runner 격리를 대신하지 않는다.

## 서버 접근 선택지

| 방식 | 평가 | 활성화 전에 필요한 승인 |
| --- | --- | --- |
| Environment-bound artifact 후 수동 helper | 현재 권고안이다. 맥미니에 inbound credential이나 runner를 추가하지 않는다. 운영자가 artifact와 GitHub 보호 기록 및 신뢰 설정을 확인해 helper를 직접 실행한다. 완전 자동은 아니다. | 서버의 정확한 값 제한 조회, helper·설정 설치 위치, 유지보수 창과 실제 실행 명령 승인 |
| GitHub-hosted runner에서 forced-command 원격 접근 | 서버에서 임의 shell 대신 사전 설치한 helper에 검증된 digest만 전달할 수 있다. 전용 계정, no-PTY·forwarding 금지, 짧은 credential 또는 제한된 secret이 필요하다. | 네트워크 노출 방식, 계정·키 또는 broker, environment secret, forced-command 설치 승인 |
| 격리된 self-hosted runner | inbound 연결이 불가능할 때만 검토한다. 앱 서버와 분리한 전용 runner, 선택된 workflow/ref 제한, 가능한 경우 job당 ephemeral 실행이 필요하다. | runner 설치·그룹·label·계정·격리와 업데이트 운영 승인 |

맥미니 자체에 Docker 권한을 가진 범용 runner를 두고 PR이나 저장소 checkout을 실행하는 방식은 채택하지 않는다. 어떤 자동화 방식에서도 서버는 PR 코드를 checkout·build·실행하지 않고 사전 설치한 helper에 승인된 digest만 전달한다.

## 호스트 helper의 동작

[dev_deploy.py](../scripts/dev_deploy.py)는 [비활성 예제 설정](../deploy/dev-host-config.example.json)과 environment-bound request JSON을 입력으로 받는다. 예제는 `enabled=false`, `admission_control=unconfigured`, `request_authentication=unconfigured`이며 실제 경로를 포함하지 않는다. 현재 schema는 `request_authentication=unconfigured` 외 값을 허용하지 않고 CLI `apply`도 이를 이유로 항상 거부한다. 원본 인증 후속 구현을 검토·병합하기 전에는 설정 편집만으로 활성화할 수 없다.

`plan`은 설정과 request 형식만 확인하고 Docker를 호출하지 않는다. 향후 `apply`를 열려면 다음 조건을 모두 구현해야 한다.

1. 별도 호스트 설정의 `enabled=true`
2. `admission_control=external-maintenance`
3. `--confirm-maintenance-window`
4. GitHub run·environment 보호·artifact 원본의 독립 인증과 replay 방지
5. 절대 경로로 고정한 검토된 Docker binary, 소유자 전용 Docker config와 승인된 context endpoint
6. 절대 경로인 신뢰 설정·검증 대상 request·Compose·기존 env 파일
7. 소유자만 읽을 수 있는 env 파일과 그룹·기타 사용자가 쓸 수 없는 설정 파일
8. 호스트 lock을 확보한 단일 실행

helper가 수행하도록 준비한 순서는 다음과 같다.

1. 호출자 `PATH`·`HOME`·`DOCKER_CONFIG`·`XDG_CONFIG_HOME`을 상속하지 않고, 설정에 고정한 절대 Docker binary와 소유자 전용 config를 사용한다. Context endpoint가 승인값과 같은지 확인한다.
2. 실제 환경을 출력하지 않는 `docker compose config --quiet`와 image 이름만 출력하는 `config --images`를 실행한다. 대상 Compose는 `CONAN_IMAGE`를 유일한 service image로 해석해야 한다.
3. 기존 컨테이너 image ID를 기록하고 요청 digest를 pull한다.
4. 대상이 `linux/arm64`이며 OCI revision 라벨이 request의 source SHA와 같은지 확인한다.
5. `/ready`를 여러 번 확인해 `inflight_urls=0`, `backlog_size=0`인지 검사한다.
6. 기존 image ID에 충돌 가능성이 낮은 로컬 rollback 태그를 추가한다.
7. 지정한 Compose project의 서비스 하나만 stop하고 `--no-build --no-deps --pull never`로 대상 digest를 기동한다.
8. Docker health, 내부 `/health`, `/ready`, 실제 실행 image ID를 확인한다.
9. 실패하면 보존한 이전 image로 같은 서비스만 다시 기동하고 동일 검사를 수행한다.

helper는 `down`, `prune`, `remove-orphans`, 볼륨 삭제, 네트워크·Tunnel·DNS 변경을 실행하지 않는다. Compose 파일과 env 파일을 생성·출력·수정하지 않으며 `CONAN_IMAGE`만 해당 Compose 프로세스 환경에서 일시적으로 덮어쓴다. `DEEPCHECK_CAPTION_POLICY`를 포함한 앱 설정은 덮어쓰지 않는다. Lock은 `O_NOFOLLOW|O_CLOEXEC`로 열고 descriptor의 종류·소유자·권한을 다시 확인하며 파일 내용을 truncate하거나 쓰지 않는다. Config·request·Compose·env·Docker binary·lock이 같은 inode 또는 경로를 가리키면 실행 전에 거부한다.

## 진행 작업 보호와 남은 한계

현재 `/ready`는 작업 수를 보여주지만 신규 접수를 닫지 않는다. `0건 확인 → 컨테이너 교체` 사이에 새 POST가 들어올 수 있다. 과거 개발계 이미지의 종료 동작도 현재 main과 같다고 가정할 수 없다. 따라서 idle 이중 확인만으로 무손실 진행 작업 보호가 완료됐다고 표현하지 않는다.

`inflight_urls=0`, `backlog_size=0`은 완료된 결과가 모두 사용자에게 전달됐다는 뜻도 아니다. 완료 job과 결과는 현재 프로세스 메모리에 남고 재시작하면 사라지므로, 분석이 막 끝났지만 FE가 아직 마지막 poll을 하지 않은 경우 결과 조회가 실패할 수 있다. 활성화 전 drain/ack 또는 결과 영속화 정책에서 이 구간도 처리해야 한다.

실제 `apply` 전에 다음 중 하나를 선택해야 한다.

1. 유지보수 창 동안 edge 또는 FE에서 신규 접수를 차단하고 차단 상태를 별도로 확인한다.
2. 별도 코드 작업과 승인을 거쳐 admission-drain 기능을 구현한다.
3. 개발계의 잔여 경쟁과 요청 실패 가능성을 명시적으로 수용하고 best-effort idle 검사만 사용한다.

현재 helper는 1번 선택만 `external-maintenance`로 표현한다. 차단 기능 자체는 실행하지 않으며 운영자가 외부 차단을 확인해야 한다. 2번은 이 CD 브랜치의 소유 범위 밖이고, 3번은 보호 완료로 볼 수 없어 구현하지 않았다.

이미지 override도 env 파일에 영속화하지 않는다. 배포 뒤 다른 사람이 기존 env만으로 Compose를 다시 실행하면 이전 이미지로 돌아갈 수 있다. 성공 digest를 저장할 비밀 없는 전용 상태 파일 또는 승인된 env의 해당 항목 수정 중 하나를 선택하기 전에는 자동 배포를 활성화하지 않는다.

Helper는 Python 예외와 `KeyboardInterrupt`가 발생하면 mutation 시작 이후 rollback을 시도하지만, `SIGKILL`, 호스트 전원 장애, 프로세스 강제 종료를 넘는 durable journal과 시작 시 reconcile은 구현하지 않았다. 대상 컨테이너 기동 뒤 프로세스가 사라지면 자동 복구를 보장할 수 없다.

### 활성화 금지 조건

다음 다섯 항목이 구현되고 별도로 검증되기 전에는 예제의 `enabled=false`를 바꾸거나 workflow를 서버 실행에 연결하지 않는다.

1. Request 소비자가 GitHub의 exact workflow run·attempt, 성공 결론, `conan-development` deployment/environment gate와 artifact 원본을 독립 인증한다.
2. Helper가 신규 접수 차단 상태를 관측하거나, 애플리케이션의 admission-drain이 마지막 idle 확인부터 stop까지 새 작업을 원자적으로 막는다.
3. Mutation 전 durable journal을 쓰고, `SIGTERM`·호스트 장애·재시작 뒤 이전/대상 image 상태를 판별해 안전하게 reconcile한다.
4. 성공한 digest와 마지막 적용 run ID·attempt를 Compose 재생성에도 유지되는 비밀 없는 신뢰 상태에 원자적으로 영속화하고, 재사용·구버전 요청을 기본 거부하되 명시적 rollback과 구분한다.
5. 신뢰 wrapper가 단 하나의 고정 config와 lock 경로만 허용해 같은 project/service가 다른 lock 파일로 동시에 실행되지 않게 한다.

## 활성화 전 사용자 결정과 승인

위 활성화 금지 조건의 설계가 먼저 정해져야 실제 연결을 구현할 수 있다.

1. 첫 단계 접근 방식은 수동 helper, forced-command 원격 접근, 격리 self-hosted runner 중 무엇인가.
2. `conan-development` required reviewer는 누구인가. prevent self-review와 관리자 우회 금지를 적용할 수 있는가.
3. 유지보수 창의 신규 접수를 어디에서 어떻게 차단하고 해제를 확인할 것인가.
4. 승인된 제한 조회로 실제 Docker context, Compose 파일, project, env 파일, 서비스명과 현재 image ID를 다시 확인해도 되는가. 환경 파일 내용과 키는 출력하지 않는다.
5. 새 배포 주체의 GHCR pull 인증은 필요한가. 필요하면 어떤 최소 권한 credential을 어디에 보관할 것인가.
6. 성공 digest를 어떤 비밀 없는 상태 파일에 영속화할 것인가. 기존 env 파일의 전체 덮어쓰기는 허용하지 않는다.
7. idle 검사 횟수·간격, 기동 health timeout, stop·개별 명령 timeout과 자동 rollback 기준은 무엇인가.
8. Artifact attestation을 사용 가능한 plan과 검증 도구가 있는가.
9. 자막 기본 `off` 브랜치가 main에 병합된 뒤 배포할 것인가. 기존 서버 env에 `manual`이 명시돼 있으면 코드 기본값보다 우선하므로 해당 한 항목의 확인·전환을 별도 승인할 것인가.
10. 유지보수 차단 해제 뒤 새 요청 수락과 결과 poll을 누가 확인할 것인가. Rollback의 구버전 `/ready`에는 `accepting_jobs`가 없을 수 있어 helper만으로 해제 후 수락을 증명하지 않는다.

이 결정이 팀 논의를 필요로 하면 docs 담당자가 `project/project-plan.md` 미결표에 발의자·담당자·상태·원문 링크를 등록해야 한다. 이 CD 런북이나 평가 기록만으로 제품·운영 결정을 완료 처리하지 않는다.

## 승인 뒤의 사용 형태

다음 `plan` 명령은 형식 예시이며 현재 서버에서 실행하라는 지시가 아니다. Helper와 호스트 설정을 신뢰 경로에 설치하고 GitHub 기록과 함께 확인한 environment-bound request artifact를 받은 뒤 실제 절대 경로로만 실행한다.

```bash
python /approved/trusted-helper/scripts/dev_deploy.py \
  --config /approved/config/conan-development.json \
  --request /approved/requests/conan-development-request.json \
  plan
```

아래는 후속 원본 인증·drain·journal·영속 상태 구현이 끝난 뒤의 목표 형태다. 현재 브랜치에서는 `request_authentication=unconfigured`이므로 명령이 Docker 호출 전에 실패하며, 별도 승인만으로 실행할 수 없다.

```bash
python /approved/trusted-helper/scripts/dev_deploy.py \
  --config /approved/config/conan-development.json \
  --request /approved/requests/conan-development-request.json \
  apply --confirm-maintenance-window
```

GitHub workflow가 이 명령을 자동 호출하는 연결은 아직 없다. 서버용 신뢰 helper는 PR 작업 트리에서 직접 실행하지 않고 검토한 commit의 파일을 별도 설치한다.

## 모의 검증

2026-09-13 독립 복사본에서 다음 CD 전용 테스트를 실행했다.

```text
python -m pytest -q tests/test_release_manifest.py tests/test_dev_deploy.py tests/test_deployment_workflow.py
40 passed
```

가짜 명령 실행기로 다음을 확인했다.

- tag·다른 저장소·명령 삽입 형식 거부
- 성공한 `push/main` backend CI run과 metadata의 SHA·run·digest 일치 검증
- PR·수동 CI run과 변조된 deployment request 거부. 수동 CI metadata를 main push로 기록하지 않음
- busy 상태에서 stop 이전 중단
- 같은 image ID의 no-op
- stop timeout 또는 target health 실패 시 이전 image 복구와 복구 실패의 별도 오류
- Compose가 `CONAN_IMAGE`를 정확히 해석하지 않으면 stop 전 중단
- 승인 endpoint와 다른 Docker context, 호출자 Docker 설정 상속을 거부
- lock과 env/config/request 경로 충돌 및 lock symlink를 데이터 변경 없이 거부
- GitHub artifact 원본 인증이 미구현인 동안 CLI `apply`를 강제 차단
- Actions concurrency와 호스트 lock의 이중 동시 실행 방지
- 위험한 Compose 명령과 `DEEPCHECK_*` override 부재
- workflow가 GitHub-hosted runner와 고정 action SHA만 사용하고 서버 명령을 포함하지 않음

Workflow 시험은 YAML parser와 정적 계약 검사이며 actionlint나 실제 Actions 재실행 semantics 검증은 아니다. 전체 시험은 실제 Docker·Compose·GHCR pull·Actions 실행·environment 승인·맥미니·Cloudflare·Vercel·외부 API·실영상 검증이 아니다. 실제 서버의 경로·네트워크·볼륨·환경·진행 작업과 복구 성공도 확인하지 않았다.
