# 맥미니 배포환경 1차 점검

2026-09-13, 사용자 요청에 따른 읽기 전용 점검. 설정 변경·컨테이너 재시작·분석 요청·내부망 스캔·취약점 공격은 하지 않았다. 환경변수 전체·개인키·사용자 결과는 출력하지 않았다.

## 판단

**호스트 포트 비공개·전용 볼륨·CPU/RAM 제한 등 기본 보호가 있다. 완전 격리를 입증한 상태는 아니며, 먼저 기존 Docker의 권한과 배포 코드 보완을 진행할 근거가 확인됐다.** Vercel 사용 때문에 발생한 문제가 아니다. 침해 흔적을 조사한 보안 사고 분석이나 취약점 전수 검사는 아니다.

VM 이전은 이번 점검으로 필수라고 결론내리지 않는다. [격리 가이드라인](server-isolation.md)의 독립 VM은 선택 가능한 강화 목표다. 파일·네트워크 경계의 추가 검증 후 위험 수용 여부와 분리 수준을 결정한다.

## 실행 상태와 근거

SSH는 Tailscale 경유, Docker는 `/usr/local/bin/docker`, context는 orbstack이다. Docker 29.4.0, Linux 7.0.14-orbstack-00380-ga7e0a2dc9535를 확인했다. 이 버전 조회만으로 최신 보안 패치 여부를 판정하지 않았다.

| 항목 | 실제 조회 결과 | 의미 |
| --- | --- | --- |
| Conan | conan-staging-deepcheck-api-1, conan-be:472aff7, healthy | 저장소 최신 main과 다름 |
| 이미지 ID | sha256:4ecdd77473ce42a9dd0799e46aafb263358dcc34a22d3d88e04c3f44c5d6aed9 | 앞선 CORS 반영 때와 동일 |
| API 포트 | 8000/tcp, published binding 없음 | 호스트에 직접 publish하지 않음. 호스트/LAN 접근 전수 검증은 아님 |
| 공유 경로 | 전용 named volume → /root/.cache, RW | Conan에 호스트 홈·SSH 경로·Docker socket bind mount 없음 |
| 추가 경로 확인 | /var/run/docker.sock, /mnt/mac 없음 | 다른 우회 경로까지 검사한 것은 아님 |
| 실행 사용자 | /proc/1/status UID/GID 모두 0 | 컨테이너 root. macOS root와 동일 권한이라는 뜻은 아님 |
| privileged·장치 | false, devices 없음 | privileged 실행·직접 장치 전달 없음 |
| 보안 옵션 | CapDrop 없음, NoNewPrivs=0, Seccomp=2 | 기본 seccomp 필터는 작동. 추가 권한 축소·상승 방지는 없음 |
| 루트 파일시스템 | 쓰기 가능 | read-only 전환을 위해 캐시·임시·상태 쓰기 경로 정리 필요 |
| 프로세스 제한 | PidsLimit 미지정 | Conan 전용 PID 상한 없음. 엔진 전체 한도가 없다고 단정하지 않음 |
| 자원 상한 | CPU 2개, RAM 3GiB | 실제 적용됨 |
| 자원 순간값 | CPU 0.29%, RAM 1.308GiB, PID 23 | 피크·부하 시험 결과가 아님 |
| 엔진 자원 | CPU 10, MemTotal 약 7.817GiB | 맥미니 물리 16GB와 Docker에 보이는 메모리는 다름 |
| 로그 | json-file, 10m × 5 | 컨테이너 로그 순환 설정 있음 |
| 환경 파일 | .env, .env.home 모두 0600 | 내용은 읽지 않고 권한만 확인 |
| 디스크 | 호스트 여유 약 86GiB; 컨테이너 /tmp 파일시스템 여유 약 84.8GB | 순간 여유. 다운로드별 용량 한도·Conan 전용 디스크 quota를 입증하지 않음 |
| 작업 상태 | completed 11, inflight 0, backlog 0 | 결과 내용 미조회. 품질 검증 수치가 아님 |
| 다른 서비스 | 기존 7개 컨테이너 running | 앱 내부 기능·보안 검증은 아님 |

## 네트워크 관계

- Conan은 `conan-staging-ingress` 한 네트워크에만 연결된다. bridge, Internal=false, IPv6 비활성, 별도 옵션은 없다.
- Laravel은 `lunchpick_app_net`·`lunchpick_monitoring`, Redis도 두 네트워크에 연결된다. Prometheus·Grafana·exporter는 monitoring에 연결된다.
- 기존 `lunchpick-tunnel-cloudflared-1`은 **세 네트워크 모두** 연결돼 있다. Conan과 Laravel이 직접 같은 네트워크에 붙은 것은 아니다.
- 공유 커넥터는 공통 의존 지점이다. 하지만 다중 네트워크 연결만으로 커넥터가 트래픽을 라우팅하거나 Conan이 Redis에 접근할 수 있다고 단정할 수 없다.
- 실제 방화벽·패킷 경로·호스트 별칭·LAN/Tailscale 접근 차단은 미검증이다. Internal=false를 내부망 무제한 접근의 증거로 쓰지도 않는다. 다음 검증은 지정한 무해한 대상에 대한 제한된 연결 검사로 진행한다.

## 배포 코드 차이

서버 Git HEAD와 컨테이너 소스를 확인했다. 배포 버전은 472aff7이며 실제 analyze 함수는 `_is_http_url`로 http(s) 여부만 검사한다. 실제 `/api/jobs`와 `/api/sessions/{session_id}` 함수에는 앱 자체 접근 제어·디버그 비활성 분기가 없다. 바깥의 Cloudflare Access 보호와 구분해야 한다.

최신 로컬 코드에는 YouTube 정규화·일부 입력 제한·목록 기본 비활성 등 보완이 있지만 현재 서버에는 반영되지 않았다. 새 CD Compose의 cap_drop ALL·no-new-privileges도 현재 실행 컨테이너에는 없다. 저장소 파일을 수정하는 것만으로 배포 상태가 바뀌지 않는다.

## 다음 작업의 우선순위

| 순서 | 작업 | 완료 조건 |
| --- | --- | --- |
| 1 | 기존 보호를 유지하며 검증한 코드로 배포 준비 | 현재 코드와 차이·결과 보존·이미지 검증·롤백을 확인. 이번 점검에서 업그레이드하지 않음 |
| 2 | Docker 실행 권한 보완 | cap_drop·no-new-privileges·PID 제한, non-root·쓰기 경로 변경을 별도 환경에서 모델/분석/상태 저장까지 검증 |
| 3 | 실제 네트워크 경계 확인 | 승인한 대상만 사용하여 기존 서비스·host·LAN·Tailscale·DNS/리다이렉트 경로 검사. 정책 로그와 비교 |
| 4 | 시간·디스크 제한 및 복구 | 작업이 실제 종료되고 임시 공간을 회수하는지, 기존 서비스가 영향받지 않는지 확인 |
| 5 | 필요한 격리 수준 결정 | 위 결과에 따라 전용 커넥터·독립 VM 또는 외부 서버 선택. Access 해제는 별도 공개 검토 |

이번 결과는 ISO-01의 실행 컨테이너 설정 감사 부분이다. macOS/OrbStack 공유 설정 전체, 호스트 방화벽, Tailscale ACL, 이미지 CVE 및 실제 차단 시험은 남았다. 따라서 ISO-01 전체와 ISO-04 격리 검증을 완료 처리하지 않는다.
