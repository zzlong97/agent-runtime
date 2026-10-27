"""只包含固定文案与稳定错误码的远程失败。"""

from agent_runtime.core.errors import ApplicationError


class RemoteA2AError(ApplicationError):
    """禁止携带远程响应、URL、用户输入或凭据的受控错误。"""


def protocol_error() -> RemoteA2AError:
    """不把底层解析错误中的原始数据带出信任边界。"""
    return RemoteA2AError(
        code="CAPABILITY_REMOTE_PROTOCOL_ERROR",
        message="远程协议不符合当前接入要求",
        retryable=False,
    )


def auth_error() -> RemoteA2AError:
    """固定认证失败不携带环境变量值或响应体。"""
    return RemoteA2AError(
        code="CAPABILITY_REMOTE_AUTH_FAILED",
        message="远程能力认证失败",
        retryable=False,
    )


def unavailable_error() -> RemoteA2AError:
    """仅表达可重试的传输暂态；调用方仍须遵守幂等与未知结果规则。"""
    return RemoteA2AError(
        code="CAPABILITY_REMOTE_UNAVAILABLE",
        message="远程能力暂时不可用",
        retryable=True,
    )
