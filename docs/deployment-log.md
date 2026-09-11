# 맥미니 배포 점검 기록

## 2026-09-11 staging 내부 기동 완료

사용자가 배포 시작을 요청했다. 프론트 배포처·주소는 아직 확정되지 않았고 DeepSeek·NAVER 키는 사용자가 직접 입력하기로 했다. 개발계/운영계와 GitHub Actions 구성은 아래 제안으로 남긴다.

| 항목 | 실제 확인값 |
| --- | --- |
| 코드 | main 472aff7201a834102d6ec2c27096dabb22a5ae4f, 추적 파일 변경 없음 |
| 이미지 | conan-be:472aff7, linux/arm64, 약 2.03GB |
| 이미지 ID | sha256:4ecdd77473ce42a9dd0799e46aafb263358dcc34a22d3d88e04c3f44c5d6aed9 |
| 기동 시각 | 2026-09-11 00:05:13 KST (2026-09-10T15:05:13Z) |
| 프로젝트·컨테이너 | conan-staging / conan-staging-deepcheck-api-1 |
| 전용 자원 | conan-staging-ingress 네트워크, conan-staging_model-cache 볼륨 |
| 실행 제한 | CPU 2개, 메모리 3GiB, uvicorn 1 프로세스, 분석 worker 1개 |
| 노출 | 호스트 published port 없음. 기존 Tunnel 연결 및 본인 이메일 한정 Access 적용 완료 |
| 상태 | /health ok, /ready ready, Docker healthy, 재시작 0, OOMKilled false |
| 모델 | MediaPipe 얼굴 검출기 초기화 성공, torch 2.14.0+cpu / CUDA 없음, ViT와 Whisper small CPU/int8 로딩 성공 |
| 로딩 시간 | 별도 검증 프로세스에서 ViT까지 13.6초, Whisper까지 누적 24.7초. 실제 영상 처리 시간 아님 |
| 메모리 관측 | 모델 검증 프로세스 종료 뒤 컨테이너 약 65MiB. 피크 및 실제 분석 메모리는 미측정 |
| 기존 서비스 | 기존 7개 컨테이너 Up 유지. Tunnel 재시작 없이 Conan 네트워크만 추가 연결하고 Compose에 영속화 |

검증 프로세스는 종료됐다. 모델 파일 캐시는 유지되지만 API 프로세스의 모델 객체가 미리 로딩된 것은 아니므로 첫 실제 요청의 모델 메모리 로딩 시간은 별도로 측정한다. 실제 Shorts E2E, DeepSeek·NAVER 인증, Access 로그인 후 API 응답, FE CORS 검증은 미완료다.

서버 be/.env.home을 새로 만들고 권한 600 및 Git ignore를 확인했다. DEEPCHECK_LLM_API_KEY, DEEPCHECK_NAVER_CLIENT_ID, DEEPCHECK_NAVER_CLIENT_SECRET은 점검 시 비어 있다. 키 값은 채팅·문서·Git에 넣지 않는다. CONAN_NETWORK는 conan-staging-ingress, CONAN_IMAGE는 conan-be:472aff7이다. 사용자가 키를 저장하면 아래 up 명령으로 컨테이너 환경에 반영한다. 기존 컨테이너의 단순 restart는 변경된 env_file 값을 반영하지 않는다.

다음 명령은 서버 be 저장소 루트에서 실행한다. 기존 문서의 기본 conan-home 명령과 구분해 반드시 -p conan-staging을 넣는다.

```bash
docker compose -p conan-staging --env-file .env.home -f compose.home.yml config --quiet
docker compose -p conan-staging --env-file .env.home -f compose.home.yml up -d --no-build deepcheck-api
docker compose -p conan-staging --env-file .env.home -f compose.home.yml ps
docker compose -p conan-staging --env-file .env.home -f compose.home.yml stop deepcheck-api
```

마지막 stop은 중지·복구가 필요할 때만 실행한다. 첫 배포라 이전 검증 이미지가 없으며 현재 이미지와 모델 볼륨을 보존한다.

### Cloudflare 연결 완료

사용자 승인으로 `conan-api-dev.dotseven.cloud` 전체 경로에 Access 앱 `Conan API Dev`를 생성했다. `Conan Dev Owner Only` 정책은 Allow / Include Emails에 사용자가 지정한 본인 이메일 한 개만 포함한다. 저장 후 정책을 다시 열어 확인했다. 이메일 원문은 이 문서에 기록하지 않는다. 세션은 24시간이며 MFA 요구는 추가하지 않았다. API hostname을 프론트 CORS Origin으로 대신 입력하지 않는다.

Access 앱을 먼저 생성한 뒤 기존 `dotseven-server` Tunnel에 hostname route를 추가했다. DNS 레코드 자동 생성과 저장 성공을 대시보드에서 확인했다. 기존 SSH·lunchpick·monitoring 경로와 catch-all은 유지했다.

기존 cloudflared를 `conan-staging-ingress`에 무중단 연결하고 staging 목적지를 `http://conan-staging-deepcheck-api-1:8000`으로 설정했다. 기존 Tunnel 관리 디렉터리의 `docker-compose.yml`에는 서비스 network와 external network 선언만 추가했으며 `config --quiet` 검증을 통과했다. 토큰이 포함된 해당 파일은 이 저장소에 복사하지 않는다. 향후 운영 네트워크도 같은 Tunnel에 연결할 때 공통 별칭 conan-api가 중복되지 않도록 환경별 고유 컨테이너명을 사용한다.

검증 결과:

- Tunnel 컨테이너의 네트워크 네임스페이스에서 staging `/health`가 `{"status":"ok"}`로 응답했다. 임시 진단 컨테이너는 실행 후 자동 삭제됐다.
- 공개 DNS 1.1.1.1과 8.8.8.8에서 A 레코드가 확인됐다. 맥미니 기본 resolver에서는 아직 이름 해석에 실패했다. 캐시 또는 전파 지연 가능성이 있으나 원인은 확정하지 않았다.
- 공개 DNS 응답 IP를 curl `--resolve`에 지정하고 TLS 인증서 검증을 유지한 HTTPS 요청에서 미인증 GET `/health`, GET `/api/jobs`, POST `/api/analyze` 모두 HTTP 302로 Access 로그인 페이지로 이동했다.
- 저장된 단일 이메일 정책과 staging healthy 상태를 확인했다. 본인 로그인 후 응답, 다른 이메일 로그인 거부, 실제 분석은 아직 시험하지 않았다. API 키 세 항목은 앞선 점검에서 비어 있었으며 사용자 입력 후 재확인이 필요하다.

대시보드 경로는 공식 문서 기준으로 Zero Trust > Access controls > Applications에서 Self-hosted and private 앱과 public hostname·Allow 정책을 설정한다. 이후 Networking > Tunnels > 기존 Tunnel > Routes > Add route > Published application에서 hostname과 위 Service URL을 입력한다. UI 버전에 따라 Zero Trust > Networks > Connectors 아래에 기존 Tunnel이 보일 수 있다. Cloudflare 관리 도메인의 대시보드 route 생성은 DNS도 자동 연결하므로 같은 이름의 레코드를 중복 생성하지 않는다.

출처: [Access 앱](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/), [Tunnel 경로](https://developers.cloudflare.com/tunnel/setup/), [DNS 자동 생성](https://developers.cloudflare.com/tunnel/routing/). 브라우저 FE 연동 시 Access cookie·OPTIONS와 CORS를 별도 확인하며 서비스 토큰을 프론트에 저장하지 않는다.

### 개발계·운영계 및 CI/CD 제안

지금은 staging 1개만 실행한다. 운영계는 검증된 동일 이미지 digest를 별도 Compose 프로젝트 conan-production으로 승격하는 방식을 제안한다. 두 환경은 배포 디렉터리·.env.home·네트워크·볼륨·API hostname을 각각 분리한다. 같은 맥미니이므로 호스트 장애와 CPU·메모리 경합까지 격리되지는 않는다. 두 분석의 동시 피크를 측정하기 전에는 개발계를 필요할 때만 실행한다. 운영계는 아직 생성하지 않았다.

GitHub Actions는 PR 테스트와 main의 Linux ARM64 이미지 빌드·GHCR 저장을 GitHub-hosted runner에서 처리하고, staging에서 검증한 이미지를 수동 지정해 운영계에 배포하는 구성이 적절하다. 맥미니에서는 이미지 실행 위주로 처리한다. 자동 배포는 고정 digest만 실행하는 제한된 배포 경로와 rollback을 확인한 후 추가한다. 실제 workflow나 runner 등록은 이번 작업에서 하지 않았다.

ubuntu-24.04-arm은 GitHub가 제공하는 ARM64 runner다. 운영 Docker 소켓을 가진 self-hosted runner에서 임의 PR 코드를 실행하면 기존 서비스에도 영향을 줄 수 있어 권장하지 않는다. GitHub Environments의 승인 기능은 저장소 공개 여부·플랜에 따른 지원 범위를 확인하고 사용한다. 출처: [GitHub-hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners), [runner 보안](https://docs.github.com/en/actions/reference/security/secure-use), [배포 환경](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).

## 2026-09-10 사전 점검

- 상태: `In Review`
- 요청: 사용자가 기존 맥미니·Docker·Cloudflare Zero Trust 기반 배포를 진행할 의사를 밝히고 기존 배포 문서에 따른 가능 여부 확인을 요청했다.
- 구현 기준: 원격 main `472aff7201a834102d6ec2c27096dabb22a5ae4f`.
- 범위: 서버·기존 컨테이너의 읽기 전용 조회, 비밀값 없는 Compose 정적 검증, 감사 결과 문서화. 이미지 빌드·서비스 기동·네트워크 연결·Cloudflare 설정 변경은 실행하지 않았다.
- 절차: [배포 인수인계](deployment-mac-mini.md). 제품·코드 차이의 상세 기준은 [백엔드 감사](https://github.com/Dynamic-Juo/docs/blob/docs/backend-deployment-audit/evaluations/2026-09-10-backend-deployment-audit.md)다. 링크 대상은 함께 공유하는 문서 브랜치이며 main 병합과 구분한다.

## 실제 환경

| 항목 | 확인값 | 확인 방법과 해석 |
| --- | --- | --- |
| 호스트 | arm64, Mac16,10, 물리 메모리 16GiB | uname과 sysctl. 사용자 지정 맥미니 환경과 대조 |
| Docker | context orbstack, Engine 29.4.0, linux/arm64 | docker context show, docker version |
| Compose | v5.1.2 | 현재 compose.home.yml 파싱 가능 |
| Docker VM 자원 | CPU 10개, 메모리 8,393,289,728 bytes, 약 7.8GiB | docker info. 물리 메모리 16GiB 전부를 컨테이너가 쓰는 설정이 아님 |
| 디스크 | 해당 APFS Data 볼륨 가용 약 94GiB | df -h. 빌드 시점 여유 재확인 필요 |
| 기존 실행 서비스 | 7개 | 아래 목록. Conan 실행 서비스 없음 |
| 현재 컨테이너 사용량 | 메모리 합계 약 288MiB, 개별 CPU 약 0~0.82% | docker stats --no-stream 1회. 피크·호스트·VM 총 메모리 수치가 아님 |
| Conan 배포 자산 | 이미지, conan-ingress 네트워크, 컨테이너 없음 | docker image ls, network ls, ps |
| 서버 환경 파일 | be/.env와 be/.env.home 없음 | 파일 존재 여부만 확인. 키 값 출력 없음 |

실행 중인 컨테이너는 lunchpick-app-1, lunchpick-nginx-exporter-1, lunchpick-redis-exporter-1, lunchpick-redis-1, lunchpick-tunnel-cloudflared-1, lunchpick-monitor-prometheus-1, lunchpick-monitor-grafana-1이다. 각 컨테이너는 점검 당시 Up 상태였고 Redis healthcheck는 healthy였다. 다른 컨테이너의 응용 계층 정상 동작을 별도로 검증한 것은 아니다. 호스트 published port는 ps 출력에 없었다.

## 기존 Tunnel 확인

- 컨테이너: `lunchpick-tunnel-cloudflared-1`.
- Compose 프로젝트: `lunchpick-tunnel`.
- 관리 파일: `/Users/dotseven/srv/projects/lunchpick/tunnel/docker-compose.yml`. 현재 호스트에서 확인한 위치이며 다른 기기에서 그대로 사용하지 않는다.
- 현재 네트워크: `lunchpick_app_net`, `lunchpick_monitoring`.
- Compose에서 token 기반 실행을 확인했다. 로컬 config 파일 지정은 없었다. Cloudflare 측 실제 hostname 라우트와 Access 설정은 조회하지 않았다.
- Conan용 후보 연결은 `conan-ingress`에 이 컨테이너를 추가하고 서비스 목적지를 `http://conan-api:8000`으로 설정하는 방식이다. 기존 네트워크를 제거하거나 다른 hostname 라우트를 바꾸지 않는다.
- network connect는 실행 중 컨테이너 연결이고 재생성 이후까지 보장되지 않는다. 운영 Compose에도 external 네트워크를 기록해야 한다. Compose 변경을 적용하며 Tunnel이 재생성되는 경우 기존 서비스 연결에 영향을 줄 수 있으므로 별도로 계획한다.

Tunnel이 있다는 사실만으로 Conan API에 Access 인증이 적용되지는 않는다. 호스트명과 정책을 확인한 뒤 연결한다. 별도 Docker 네트워크도 내부 IP 요청을 막는 방화벽을 대신하지 않으므로 감사의 입력 검증 결함은 남는다.

## Compose 검증

격리한 작업 복사본에서 비밀값 없는 .env.home과 저장소의 .env.home.example을 사용해 다음 정적 검사를 실행했다. 운영 환경 파일을 만들거나 기존 서비스에 적용하지 않았다.

```bash
docker compose --env-file .env.home.example -f compose.home.yml config --quiet
```

렌더링된 설정에서 `conan-home`, `linux/arm64`, published ports 없음, CPU 2개, 메모리 3,221,225,472 bytes(3GiB), external conan-ingress, conan-api 별칭, /root/.cache 모델 볼륨, 영상 worker 1개, 주장 worker 3개, DeepSeek 제공자를 확인했다. 일반 config 출력은 비밀값을 노출할 수 있으므로 실제 환경에서는 --quiet를 사용한다.

구성상 기존 서비스와 포트 충돌 없이 시험 배포할 수 있다. CPU·메모리 값은 시작 설정이며 실제 모델 로딩·추론이 3GiB 이내로 동작하는지는 이 맥미니에서 아직 검증하지 않았다. 기존 handoff의 ARM64 빌드 성공은 다른 개발 기기의 기록이고 이번 서버의 빌드 성공으로 옮겨 적지 않았다.

## 다음 배포 단계와 미결 값

| 순서 | 작업 | 현재 상태·완료 조건 |
| --- | --- | --- |
| 1 | 배포 커밋·환경 고정 | 구현 main 472aff7 확인. 실제 이미지 태그·ID는 빌드 후 기록한다. 서버 .env.home에 DeepSeek·NAVER 키를 안전하게 넣고 파일 권한을 제한해야 한다. |
| 2 | ARM64 이미지 빌드 | 미실행. Conan만 빌드하며 기존 서비스 상태·자원 변화를 함께 관찰한다. |
| 3 | 내부 기동·모델 검증 | 미실행. 전용 네트워크를 만든 뒤 Conan만 기동한다. /health, 얼굴 모델 초기화, 실제 provider 활성화와 Shorts 1건을 확인한다. |
| 4 | 제한된 Tunnel 접근 | API hostname과 Access 허용 대상 미정. 기존 cloudflared의 conan-ingress 연결, 정책 적용, http://conan-api:8000 라우팅 후 HTTPS health를 검증한다. |
| 5 | FE 연결 | Vercel Origin과 인증 방식 미확인. 실제 Origin의 OPTIONS·POST·GET polling을 확인한다. Access cookie 방식은 현재 BE의 allow_credentials 설정과 FE fetch 설정까지 대조해야 한다. 서비스 토큰은 브라우저 코드에 넣지 않는다. |
| 6 | 부하·복구 확인 | 최초 모델 다운로드 분석과 캐시 후 분석 시간을 구분한다. 피크 메모리, OOM, 기존 서비스 상태, 최종 job·부분 실패를 확인한다. 첫 배포 실패 시 Conan만 stop하고 추가한 Conan 라우트·연결만 원복한다. 검증된 이전 이미지가 생기면 태그 롤백을 사용한다. |
| 7 | 일반 사용자 공개 | 입력·접근 제한, 실제 queue·timeout과 판정·상태 결함 보완 및 실제 영상 인수 검수 후 판단한다. |

API hostname, Vercel Origin, Access 정책, 이미지 ID, 기동 시각, 시험 영상 ID, 분석 시간, 피크 메모리, 복구 태그는 아직 미정 또는 미측정이다. 현재 자원 여유는 제한된 시험 배포의 근거일 뿐 서비스 성능 보장이 아니다.

## 참고

- [Docker 외부 네트워크](https://docs.docker.com/compose/how-tos/networking/)
- [Cloudflare Access와 CORS](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/cors/)

이번 점검에서는 컨테이너 전체 inspect, 환경변수 값, Tunnel token을 출력하거나 Git에 기록하지 않았다. 서비스 재시작과 전체 prune도 실행하지 않았다.
