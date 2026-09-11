"""Application-level exceptions independent of transport details."""


class ApplicationError(Exception):
    """A stable error that can later be adapted to HTTP or SSE responses."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int = 500,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
