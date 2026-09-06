"""FastAPI 백엔드 패키지.

여기서 `app`을 import하지 않는다. import만으로 Harness(워커 스레드)가 뜨면
테스트나 도구가 패키지를 건드릴 때마다 스레드가 생긴다. 진입점은
`uvicorn backend.app:app`이다.
"""
