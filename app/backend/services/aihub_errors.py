class InvalidImageInputError(ValueError):
    """Raised when the provided image input cannot be parsed."""


class InvalidAudioInputError(ValueError):
    """Raised when the provided audio input cannot be parsed."""


class InvalidPdfInputError(ValueError):
    """Raised when the provided PDF input is invalid or unsupported."""


class UpstreamError(Exception):
    """上游模型服务的可分类故障（设计文档 2026-09-20 S3.1）。

    kind 取值：
    - "auth"          401/403，鉴权失败：不重试，立即失败 + ERROR 日志
    - "rate_limit"    429：退避重试 0.5→1→2s，最多 3 次
    - "timeout"       超时 / 连接错误：同上退避重试
    - "upstream_5xx"  上游 5xx：同上退避重试
    - "empty"         模型返回空内容：重试 + 降 max_tokens 再试
    - "truncated"     产出未闭合 </html>：走续写/重跑链
    - "unknown"       映射失败：宁可多试一次，retriable=True
    """

    def __init__(
        self,
        kind: str,
        message: str | None = None,
        status_code: int | None = None,
        retriable: bool = True,
    ) -> None:
        super().__init__(message or kind)
        self.kind = kind
        self.status_code = status_code
        self.retriable = retriable
