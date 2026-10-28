"""只包含固定文案与稳定错误码的远程失败。"""

from agent_runtime.core.errors import ApplicationError


class RemoteA2AError(ApplicationError):
    """禁止携带远程响应、URL、用户输入或凭据的受控错误。"""


class RemoteExecutionPending(Exception):
    """远程结果尚未确认的内部控制信号，不能映射为执行失败。"""

    def __init__(self, code="CAPABILITY_REMOTE_STATE_UNKNOWN"):
        super().__init__("远程执行结果待确认")
        self.code = code


class RemoteInputRequired(RemoteExecutionPending):
    """仅经 Gateway 交给 Parent 的补参控制信号，内部身份不得公开。"""

    def __init__(self, payload):
        super().__init__("CAPABILITY_REMOTE_INPUT_REQUIRED")
        self.payload = payload


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
