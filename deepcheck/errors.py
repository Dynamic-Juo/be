"""도메인 예외.

실패를 문자열 하나로 뭉뚱그리면 호출자(FE·관리자·우리)가 대응을 나눌 수 없다.
"영상을 받을 수 없다"와 "우리 서버가 터졌다"는 사용자에게 다른 안내를 해야 하고,
재시도해도 되는 실패와 그렇지 않은 실패도 구분돼야 한다.

각 예외는 기계가 읽는 `code`, 사람이 읽는 메시지, 어느 단계에서 났는지(`stage`),
재시도 가능 여부(`retryable`)를 들고 다닌다.
"""

from __future__ import annotations


class DeepCheckError(Exception):
    """이 서비스가 의도적으로 올리는 모든 예외의 부모."""

    code = "internal_error"
    http_status = 500
    retryable = False

    def __init__(self, message: str, *, stage: str | None = None,
                 cause: BaseException | None = None):
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.cause = cause

    def to_dict(self) -> dict:
        # Provider/tool exceptions can contain URLs, local paths or credentials.
        # Public responses use stable messages; detailed causes stay in tracebacks.
        public_messages = {
            "internal_error": "서버 내부 오류가 발생했습니다.",
            "download_failed": "영상을 받지 못했습니다. 공개 상태와 지원 조건을 확인해주세요.",
            "transcription_failed": "음성을 텍스트로 변환하지 못했습니다.",
            "dependency_missing": "분석에 필요한 구성 요소를 사용할 수 없습니다.",
        }
        payload = {
            "code": self.code,
            "message": public_messages.get(self.code, self.message),
            "retryable": self.retryable,
        }
        if self.stage:
            payload["stage"] = self.stage
        return payload


class UnsupportedURLError(DeepCheckError):
    """http(s)가 아니거나 우리가 다룰 수 없는 URL."""

    code = "unsupported_url"
    http_status = 422


class UnsupportedVideoError(DeepCheckError):
    """URL은 정상이지만 우리가 지원하는 영상 조건에 맞지 않는다.

    M-02가 정한 지원 범위는 "주 사용 언어가 한국어이고 공개 상태가 public인
    YouTube Shorts(최대 3분)"다. 조건 밖 영상은 시스템 실패가 아니라 정상적인
    거절이므로, 다운로드 실패와 구분해서 사용자에게 이유를 알려준다.
    """

    code = "unsupported_video"
    http_status = 422


class DownloadError(DeepCheckError):
    """영상을 받지 못했다. 비공개·삭제·지역제한·네트워크 등 외부 원인이 대부분이다."""

    code = "download_failed"
    http_status = 502
    retryable = True


class TranscriptionError(DeepCheckError):
    """STT 실패. 오디오가 없거나 모델 실행에 실패한 경우."""

    code = "transcription_failed"
    http_status = 502
    retryable = True


class DependencyMissingError(DeepCheckError):
    """필요한 라이브러리·모델 파일이 설치되어 있지 않다. 재시도해도 해결되지 않는다."""

    code = "dependency_missing"
    http_status = 503


class JobNotFoundError(DeepCheckError):
    """이미 정리됐거나 존재한 적 없는 job."""

    code = "job_not_found"
    http_status = 404


class ServerBusyError(DeepCheckError):
    """대기열이 가득 찼다. 잠시 후 재시도하면 된다."""

    code = "server_busy"
    http_status = 429
    retryable = True


class SessionBusyError(DeepCheckError):
    """이 세션이 이미 분석을 하나 돌리고 있다.

    M-07이 "브라우저 세션별 활성 분석도 1건으로 제한한다"로 정했다. 대기열
    포화(ServerBusyError)와는 원인이 달라서 구분한다 — 사용자에게 "서버가
    바쁩니다"가 아니라 "먼저 하던 분석이 끝나야 합니다"라고 말해야 한다.
    """

    code = "session_busy"
    http_status = 429
    retryable = True


def as_error_dict(exc: BaseException, stage: str | None = None) -> dict:
    """예외를 응답·job 기록에 쓸 dict로 바꾼다.

    우리가 정의하지 않은 예외도 같은 모양으로 감싸서, 소비하는 쪽이 분기를 두 벌
    만들지 않게 한다.
    """
    if isinstance(exc, DeepCheckError):
        payload = exc.to_dict()
        if stage and "stage" not in payload:
            payload["stage"] = stage
        return payload
    return {
        "code": DeepCheckError.code,
        "message": "서버 내부 오류가 발생했습니다.",
        "retryable": False,
        **({"stage": stage} if stage else {}),
    }
