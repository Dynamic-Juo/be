# 참새 백엔드 전용 외부 연결 출구

**검증용 구성이다. 운영 설치·공개 전환은 아직 하지 않았다.** 실제 맥미니 결과는 [네트워크 경계 검증](../../docs/deployment-isolation-audit-2026-09-14.md)을 읽는다.

앱은 사전에 만든 `internal` 네트워크 하나에만 연결한다. 출구 프록시는 그 내부망과 별도 outbound 네트워크에 연결한다. 앱의 HTTP(S) 클라이언트는 프록시를 통하고, 직접 소켓 연결은 네트워크에서 막는다. 프록시 포트를 호스트에 게시하지 않는다. 운영용 전용 Tunnel도 이 경계를 고려해 별도로 연결한다.

`compose.example.yml`은 설치 예제이며 현재 CD 파일에 합치는 override가 아니다. 프록시는 controller가 교체하는 앱 한 서비스와 별도로 관리하고 검증한 digest를 고정한다. 현재 앱 CD의 `CONAN_NETWORK`를 바꿀 때는 실제 Internal=true를 확인하고 helper의 target/config migration 절차를 따른다.

앱 설정은 검증한 내부 프록시 주소로 `HTTP_PROXY`, `HTTPS_PROXY`를 지정하고 `NO_PROXY=localhost,127.0.0.1`만 사용한다. 이 값들은 비밀이 아니지만 프록시와 앱이 같은 내부망인지 확인해야 한다. TLS 검증은 유지한다. 각 라이브러리의 프록시 지원·모델 캐시 경로는 실제 실행에서 확인하고, 미지원이면 임의 외부망 연결로 우회하지 않는다.

Squid는 목록의 도메인으로 가는 TLS CONNECT 443만 허용하고 사설·루프백·링크 로컬·Tailscale CGNAT·예약 주소를 거절한다. 콘텐츠 캐시와 요청 URL 로그를 끄며 128 MiB·PID 32·비특권 사용자·읽기 전용 루트·제한된 tmpfs로 실행한다. 허용 목록 확대는 실제 실패 목적지와 필요성을 확인하고 코드 리뷰로 한다.

서버의 검증 디렉터리에서 `Dockerfile`과 `squid.conf`로 이미지를 빌드한 뒤, 아래처럼 명시적인 이미지와 이미 확인된 사설 테스트 대상 하나를 지정해 검사한다. 실제 운영 파일·볼륨은 전달하지 않는다.

```text
python3 scripts/verify_egress.py --docker /path/to/docker \
  --probe-image <검증한-Python-이미지> --proxy-image <검증할-프록시-이미지> \
  --private-target <연결-시험을-허가받은-사설-IP>
```

스크립트는 일회용 네트워크·컨테이너를 만들고 정리한다. NAVER API에 키 없는 읽기 요청을 보내 연결만 확인한다. 실제 분석이나 유료 API 호출은 하지 않는다. 최종 정상 시험은 허용 연결 1건과 거절 3건이며, 전체 인터넷·IPv6·DNS 재해석·리다이렉트 공격을 전수 검증한 결과가 아니다.

[Docker internal 네트워크](https://docs.docker.com/reference/compose-file/networks/#internal), [Squid 접근 규칙](https://www.squid-cache.org/Doc/config/http_access/), [목적지 주소 ACL](https://www.squid-cache.org/Doc/config/acl/)을 기준으로 구성했다. 출구 프록시 자체가 침해되거나 공유 VM 커널이 침해되는 경우까지 격리하는 구성은 아니다.
