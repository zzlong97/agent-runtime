"""MI首批执行权原语；普通单进程调度尚不使用此入口。"""

import asyncio
import logging
import math
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.logging import log_business_event

logger = logging.getLogger(__name__)

_SCHEMA_STATEMENTS = (
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS deletion_requested_at TIMESTAMPTZ NULL",
    """ALTER TABLE runs ADD COLUMN IF NOT EXISTS owner_instance_id UUID NULL,
        ADD COLUMN IF NOT EXISTS execution_epoch BIGINT NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ NULL""",
    """DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='runs'::regclass
            AND conname='runs_execution_epoch_nonnegative') THEN
            ALTER TABLE runs ADD CONSTRAINT runs_execution_epoch_nonnegative CHECK (execution_epoch>=0);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='runs'::regclass
            AND conname='runs_owner_expiry_pair') THEN
            ALTER TABLE runs ADD CONSTRAINT runs_owner_expiry_pair CHECK
                ((owner_instance_id IS NULL)=(lease_expires_at IS NULL));
        END IF;
        END $$""",
    """CREATE TABLE IF NOT EXISTS runtime_cluster_config (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id=1),
        contract_hash CHAR(64) NOT NULL CHECK (contract_hash ~ '^[0-9a-f]{64}$')
    )""",
    """COMMENT ON COLUMN sessions.deletion_requested_at IS '会话删除屏障的持久请求时间；为空表示尚未请求删除，删除完成后随会话行清理。';
        COMMENT ON COLUMN runs.owner_instance_id IS '当前执行者每次进程启动生成的实例UUID；与租约到期时间同时为空或同时存在。';
        COMMENT ON COLUMN runs.execution_epoch IS 'Run执行权代次；每次领取递增，终态保留，不进入业务幂等键或Child线程标识。';
        COMMENT ON COLUMN runs.lease_expires_at IS '依据数据库实际时钟计算的执行权到期时间；过期执行者不得续活或写入。';
        COMMENT ON TABLE runtime_cluster_config IS '保存当前Runtime集群的单一执行契约摘要；仅受控初始化写入，不保存配置正文或凭据。';
        COMMENT ON COLUMN runtime_cluster_config.singleton_id IS '执行契约单例主键，唯一允许值为1。';
        COMMENT ON COLUMN runtime_cluster_config.contract_hash IS '当前已初始化执行契约的小写SHA256摘要；领取、续租与执行写入必须严格匹配，不自动覆盖。';""",
)


class RunOwnershipLost(RuntimeError):
    """内部执行权失效信号；调用方不得据此覆盖新执行者的失败或终态。"""


class DeploymentContractMismatch(RuntimeError):
    """部署契约未初始化或不一致；不允许领取、续租和执行写入。"""


@dataclass(frozen=True, slots=True)
class RunExecutionToken:
    """仅在Runtime执行上下文中传递的不可变令牌，不属于业务State。"""

    run_id: UUID
    instance_id: UUID
    execution_epoch: int

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, UUID) or not isinstance(self.instance_id, UUID):
            raise ValueError("执行令牌必须使用Run与实例UUID")
        if type(self.execution_epoch) is not int or self.execution_epoch < 1:
            raise ValueError("执行权代次必须是正整数")


_execution_token: ContextVar[RunExecutionToken | None] = ContextVar(
    "runtime_execution_token", default=None,
)


@contextmanager
def bind_execution_token(token: RunExecutionToken) -> Iterator[None]:
    """绑定当前执行上下文；嵌套和异常退出均恢复原令牌。"""
    if not isinstance(token, RunExecutionToken):
        raise ValueError("只能绑定有效执行令牌")
    previous = _execution_token.set(token)
    try:
        yield
    finally:
        _execution_token.reset(previous)


def require_execution_token() -> RunExecutionToken:
    """缺少令牌立即拒绝执行写入，不提供通用绕过入口。"""
    token = _execution_token.get()
    if token is None:
        log_business_event(logger, "执行写入拒绝", status="rejected", error_code="RUN_OWNERSHIP_LOST")
        raise RunOwnershipLost("执行写入缺少执行令牌")
    return token


@asynccontextmanager
async def _business_boundary(operation: str, run_id: UUID | None = None):
    started = perf_counter()
    log_business_event(logger, operation + "开始", run_id=run_id)
    try:
        yield
    except BaseException as exc:
        log_business_event(
            logger, operation + ("取消" if isinstance(exc, asyncio.CancelledError) else
                                 "拒绝" if isinstance(exc, (RunOwnershipLost, DeploymentContractMismatch)) else "失败"),
            run_id=run_id, error_type=type(exc).__name__,
            error_code=("RUN_OWNERSHIP_LOST" if isinstance(exc, RunOwnershipLost) else
                        "DEPLOYMENT_CONTRACT_MISMATCH" if isinstance(exc, DeploymentContractMismatch) else
                        "RUN_OWNERSHIP_TRANSACTION_FAILED"),
            duration_ms=round((perf_counter()-started)*1000, 3),
        )
        raise
    else:
        log_business_event(logger, operation + "完成", run_id=run_id, duration_ms=round((perf_counter()-started)*1000,3))


class PostgresRunOwnershipRepository:
    """领取与执行写入独享短连接；DDL与受控初始化走明确独立入口。"""

    def __init__(self, settings: Settings | None = None, *, contract_hash: str):
        if not isinstance(contract_hash, str) or re.fullmatch(r"[0-9a-f]{64}", contract_hash) is None:
            raise ValueError("部署契约必须是小写SHA256摘要")
        self._settings = settings or get_settings()
        self._contract_hash = contract_hash

    @asynccontextmanager
    async def _connection(self, *, bounded: bool) -> AsyncIterator[AsyncConnection]:
        conn = await AsyncConnection.connect(
            self._settings.database_connection_string, autocommit=True, row_factory=dict_row,
        )
        try:
            if bounded:
                # 必须在BEGIN之前设置整体时限；不能用逐条statement_timeout替代。
                milliseconds = max(1, math.floor(self._settings.run_transaction_timeout_seconds * 1000))
                for setting, value in (
                    ("transaction_timeout", milliseconds),
                    ("statement_timeout", milliseconds),
                    ("lock_timeout", min(milliseconds, 1000)),
                    ("idle_in_transaction_session_timeout", milliseconds),
                ):
                    await conn.execute("SELECT set_config(%s,%s,false)", (setting, f"{value}ms"))
            async with conn.transaction():
                yield conn
        finally:
            # transaction_timeout会主动断开后端；任何失败连接均关闭而不归还执行流程。
            await conn.close()

    async def setup(self) -> None:
        """旧Session/Run setup后显式增量初始化；不更改框架表及普通Bootstrap。"""
        async with _business_boundary("执行权结构初始化"):
            async with self._connection(bounded=False) as conn:
                for statement in _SCHEMA_STATEMENTS:
                    await conn.execute(statement)

    async def initialize_contract(self) -> None:
        """仅受控初始化允许创建单例；已有摘要不符时绝不覆盖。"""
        async with _business_boundary("部署契约初始化"):
            async with self._connection(bounded=False) as conn:
                await conn.execute("INSERT INTO runtime_cluster_config VALUES (1,%s) ON CONFLICT DO NOTHING", (self._contract_hash,))
                await self._check_contract(conn)

    async def _check_contract(self, conn: AsyncConnection) -> None:
        row = await (await conn.execute("SELECT contract_hash FROM runtime_cluster_config WHERE singleton_id=1 FOR SHARE")).fetchone()
        if row is None or row["contract_hash"] != self._contract_hash:
            raise DeploymentContractMismatch("执行契约未初始化或与当前实例不一致")

    @staticmethod
    def _lease_seconds(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 15:
            raise ValueError("租约有效期必须大于0且不超过15秒")
        return value

    async def claim(self, run_id: UUID, instance_id: UUID, *, lease_seconds: float = 15) -> RunExecutionToken | None:
        """普通可执行状态领取；cancel_requested只授权调用方执行取消/对账。"""
        return await self._claim(run_id, instance_id, lease_seconds, reconciliation=False)

    async def claim_for_reconciliation(self, run_id: UUID, instance_id: UUID, *, lease_seconds: float = 15) -> RunExecutionToken | None:
        """仅领取interrupted用于已落盘终态对账；不得据此运行或恢复图。"""
        return await self._claim(run_id, instance_id, lease_seconds, reconciliation=True)

    async def _claim(self, run_id: UUID, instance_id: UUID, lease_seconds: float, *, reconciliation: bool) -> RunExecutionToken | None:
        lease_seconds = self._lease_seconds(lease_seconds)
        if not isinstance(run_id, UUID) or not isinstance(instance_id, UUID):
            raise ValueError("领取必须使用Run与实例UUID")
        async with _business_boundary("执行权领取", run_id):
            async with self._connection(bounded=True) as conn:
                await self._check_contract(conn)
                candidate = await (await conn.execute("SELECT session_id FROM runs WHERE run_id=%s", (run_id,))).fetchone()
                if candidate is None:
                    return None
                # 候选只读不持Run锁；实际领取严格Session→Run，竞争者跳过已锁候选。
                session = await (await conn.execute("SELECT deletion_requested_at FROM sessions WHERE session_id=%s FOR UPDATE SKIP LOCKED", (candidate["session_id"],))).fetchone()
                if session is None:
                    return None
                row = await (await conn.execute("SELECT status,owner_instance_id,lease_expires_at,execution_epoch FROM runs WHERE run_id=%s FOR UPDATE SKIP LOCKED", (run_id,))).fetchone()
                if row is None:
                    return None
                now = await self._clock(conn)
                allowed = {"interrupted"} if reconciliation else {"queued", "running", "recovering", "cancel_requested"}
                if (row["status"] not in allowed or
                    (session["deletion_requested_at"] is not None and row["status"] != "cancel_requested") or
                    (row["lease_expires_at"] is not None and row["lease_expires_at"] > now)):
                    log_business_event(logger, "执行权领取跳过", run_id=run_id, status=row["status"])
                    return None
                epoch = row["execution_epoch"] + 1
                await conn.execute("""UPDATE runs SET owner_instance_id=%s,execution_epoch=%s,
                    lease_expires_at=%s + %s * INTERVAL '1 second',updated_at=%s WHERE run_id=%s""",
                    (instance_id, epoch, now, lease_seconds, now, run_id))
                log_business_event(logger, "执行权领取落库", run_id=run_id, session_id=candidate["session_id"], instance_id=instance_id, execution_epoch=epoch)
                return RunExecutionToken(run_id, instance_id, epoch)

    @staticmethod
    async def _clock(conn: AsyncConnection):
        return (await (await conn.execute("SELECT clock_timestamp() AS actual_time")).fetchone())["actual_time"]

    async def _lock_and_validate(self, conn: AsyncConnection, token: RunExecutionToken):
        if not isinstance(token, RunExecutionToken):
            raise RunOwnershipLost("执行写入缺少有效令牌")
        await self._check_contract(conn)
        row = await (await conn.execute("SELECT owner_instance_id,execution_epoch,lease_expires_at FROM runs WHERE run_id=%s FOR UPDATE", (token.run_id,))).fetchone()
        # 锁等待可能跨过到期时间；必须锁后单独读取实际时钟。
        now = await self._clock(conn)
        if (row is None or row["owner_instance_id"] != token.instance_id or
            row["execution_epoch"] != token.execution_epoch or
            row["lease_expires_at"] is None or row["lease_expires_at"] <= now):
            raise RunOwnershipLost("Run执行权已失效")
        return now

    @asynccontextmanager
    async def guard(self, token: RunExecutionToken) -> AsyncIterator[AsyncConnection]:
        """调用方实际SQL必须使用yield的同一连接；锁一直保留到实际写入提交。"""
        async with _business_boundary("执行写入事务", getattr(token, "run_id", None)):
            async with self._connection(bounded=True) as conn:
                await self._lock_and_validate(conn, token)
                yield conn

    async def renew(self, token: RunExecutionToken, *, lease_seconds: float = 15) -> None:
        """仅匹配且未过期的代次可续租；不能复活已过期租约。"""
        lease_seconds = self._lease_seconds(lease_seconds)
        async with _business_boundary("执行权续租", getattr(token, "run_id", None)):
            async with self._connection(bounded=True) as conn:
                now = await self._lock_and_validate(conn, token)
                await conn.execute("UPDATE runs SET lease_expires_at=%s + %s * INTERVAL '1 second',updated_at=%s WHERE run_id=%s", (now, lease_seconds, now, token.run_id))

    async def release(self, token: RunExecutionToken) -> None:
        """持有者显式释放租约并保留代次；陈旧持有者不能清理新租约。"""
        async with _business_boundary("执行权释放", getattr(token, "run_id", None)):
            async with self._connection(bounded=True) as conn:
                now = await self._lock_and_validate(conn, token)
                await conn.execute("UPDATE runs SET owner_instance_id=NULL,lease_expires_at=NULL,updated_at=%s WHERE run_id=%s", (now, token.run_id))
