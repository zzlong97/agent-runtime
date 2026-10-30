"""项目自有表注释的独立预期和真实数据库核验工具，不读取实现常量。"""

import re

from psycopg import sql

from agent_runtime import feedback
from agent_runtime.capabilities import persistence
from agent_runtime.persistence.database import open_database_connection
from agent_runtime.runtime import event_repository, interrupts, repository
from agent_runtime.sessions import repository as sessions


EXPECTED_COMMENTS = {
    "sessions": "保存 Session 业务元数据；每条记录标识一个固定用户的会话，并作为该会话业务数据与删除流程的根记录。",
    "message_feedback": "保存用户对已完成 AI 回答的最终点赞或点踩值；session_id 用于会话删除时清理。",
    "runs": "保存每次普通消息或重新生成请求的持久执行生命周期、幂等与恢复投影；是 Run 状态的 PostgreSQL 权威数据源。",
    "runtime_events": "保存 Run 的持久公开事件和内部事件，按 Run 序号记录执行、Interrupt、Capability Invocation 与 Task 生命周期投影；不保存瞬时消息增量或完整 AI 消息正文。",
    "run_interrupts": "保存 Run 的中断及恢复幂等投影，包括中断状态、恢复输入和恢复请求标识；每个 Run 同时最多一个待处理中断。",
    "capability_tasks": "保存 Capability Task 的生命周期、所属 Session 与 Capability、状态 Schema 版本及最近一次 Run/Invocation 投影；不保存业务载荷。",
    "capability_task_contexts": "保存每个 Session 与 Capability 当前选中的 active Capability Task 指针；没有当前 Task 时不保留记录。",
    "capability_operations": "保存 Capability 外部副作用操作的幂等账本状态，关联 Run、Invocation 和 Task；不保存业务响应载荷。",
    "user_capability_permissions": "保存固定用户对 Capability 的显式允许或拒绝记录；无记录按拒绝处理，记录不随 Session 删除。",
}

# 字段名来自批准的现有结构；框架表不在项目注释契约内。
EXPECTED_COLUMNS = {
    "sessions": "session_id user_id title created_at updated_at".split(),
    "message_feedback": "user_id session_id message_id value updated_at".split(),
    "runs": (
        "run_id request_id session_id thread_id parent_run_id run_type input_message_id "
        "response_message_id start_checkpoint_id input_payload request_fingerprint status "
        "recovery_attempts seq_high_watermark error_code error_message created_at started_at "
        "finished_at updated_at"
    ).split(),
    "runtime_events": (
        "event_id run_id seq event_type source visibility payload schema_version durability created_at"
    ).split(),
    "run_interrupts": (
        "run_id interrupt_id status interrupt_payload resume_payload resume_request_id "
        "resume_request_fingerprint created_at resumed_at cancelled_at"
    ).split(),
    "capability_tasks": (
        "task_id session_id capability_id state_schema_version status last_run_id "
        "last_invocation_id created_at updated_at ended_at"
    ).split(),
    "capability_task_contexts": "session_id capability_id current_task_id updated_at".split(),
    "capability_operations": (
        "operation_id run_id invocation_id task_id capability_id operation_key "
        "idempotency_key status created_at updated_at"
    ).split(),
    "user_capability_permissions": "user_id capability_id allowed created_at updated_at".split(),
}

REPOSITORIES = (
    (sessions, sessions.PostgresSessionRepository, "sessions"),
    (feedback, feedback.PostgresFeedbackStore, "message_feedback"),
    (repository, repository.PostgresRunRepository, "runs"),
    (event_repository, event_repository.PostgresRuntimeEventRepository, "runtime_events"),
    (interrupts, interrupts.PostgresRunInterruptRepository, "run_interrupts"),
    (persistence, persistence.CapabilityTaskRepository, "capability_tasks"),
    (persistence, persistence.CapabilityTaskContextRepository, "capability_task_contexts"),
    (persistence, persistence.CapabilityOperationRepository, "capability_operations"),
    (persistence, persistence.UserCapabilityPermissionRepository, "user_capability_permissions"),
)


async def setup_all(settings):
    """按正常依赖顺序调用业务初始化，Run 内部已初始化 Interrupt。"""

    for _, factory, table in REPOSITORIES:
        if table != "run_interrupts":
            await factory(settings).setup()


async def snapshot(settings):
    """仅导出数据库结构、注释、行数和数据摘要，不导出业务正文。"""

    async with open_database_connection(settings) as connection:
        await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        await connection.execute("SET TIME ZONE 'UTC'")
        await connection.execute("SET DateStyle = 'ISO, YMD'")
        await connection.execute("SET extra_float_digits = 3")
        result = {}
        queries = {
            "tables": """
                SELECT c.relname, pg_get_userbyid(c.relowner) AS owner,
                       c.relacl::text AS acl
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                ORDER BY c.relname
            """,
            "columns": """
                SELECT c.relname, a.attname, a.attnum,
                       format_type(a.atttypid, a.atttypmod) AS type,
                       a.attnotnull, pg_get_expr(d.adbin, d.adrelid) AS default_expr,
                       a.attidentity, a.attgenerated
                FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                  AND a.attnum > 0 AND NOT a.attisdropped
                ORDER BY c.relname, a.attnum
            """,
            "constraints": """
                SELECT c.relname, p.conname, pg_get_constraintdef(p.oid) AS definition,
                       p.convalidated, p.condeferrable, p.condeferred
                FROM pg_constraint p JOIN pg_class c ON c.oid = p.conrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' ORDER BY c.relname, p.conname
            """,
            "indexes": """
                SELECT c.relname, i.relname AS index_name,
                       pg_get_indexdef(x.indexrelid) AS definition,
                       x.indisvalid, x.indisready
                FROM pg_index x JOIN pg_class c ON c.oid = x.indrelid
                JOIN pg_class i ON i.oid = x.indexrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' ORDER BY c.relname, i.relname
            """,
            "comments": """
                SELECT c.relname AS table_name,
                       obj_description(c.oid, 'pg_class') AS comment
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                ORDER BY c.relname
            """,
            "column_comments": """
                SELECT c.relname AS table_name, a.attname AS column_name,
                       col_description(c.oid, a.attnum) AS comment
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = c.oid
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                  AND a.attnum > 0 AND NOT a.attisdropped
                ORDER BY c.relname, a.attnum
            """,
            "excluded_comments": """
                SELECT pg_describe_object(d.classoid, d.objoid, d.objsubid) AS object,
                       d.description
                FROM pg_description d
                CROSS JOIN LATERAL pg_identify_object(d.classoid, d.objoid, d.objsubid) obj
                WHERE (obj.schema IS NULL OR obj.schema NOT IN
                    ('pg_catalog', 'information_schema', 'pg_toast'))
                  AND NOT (d.classoid = 'pg_class'::regclass
                    AND d.objoid IN (SELECT c.oid FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'public' AND c.relname = ANY(%s)))
                ORDER BY object, d.description
            """,
        }
        for name, query in queries.items():
            cursor = await connection.execute(
                query, (list(EXPECTED_COMMENTS),) if name == "excluded_comments" else None
            )
            result[name] = await cursor.fetchall()
        result["data"] = {}
        for table in EXPECTED_COMMENTS:
            cursor = await connection.execute(sql.SQL("""
                SELECT count(*) AS rows,
                       md5(coalesce(string_agg(row_hash, '' ORDER BY row_hash), '')) AS digest
                FROM (SELECT md5(to_jsonb(t)::text) AS row_hash FROM public.{} t) hashed
            """).format(sql.Identifier(table)))
            result["data"][table] = await cursor.fetchone()
        return result


def assert_comments(value):
    """逐字核对九条表注释，并核对79个自有字段的真实中文注释。"""

    comments = {row["table_name"]: row["comment"] for row in value["comments"]}
    assert {table: comments.get(table) for table in EXPECTED_COMMENTS} == EXPECTED_COMMENTS
    actual = {
        (row["table_name"], row["column_name"]): row["comment"]
        for row in value["column_comments"] if row["table_name"] in EXPECTED_COLUMNS
    }
    assert set(actual) == {
        (table, column) for table, columns in EXPECTED_COLUMNS.items() for column in columns
    }
    missing = [f"{table}.{column}" for (table, column), comment in actual.items()
               if not comment or not re.search(r"[\u4e00-\u9fff]", comment)]
    assert not missing, missing


def assert_unchanged(before, after):
    """自有表/字段注释以外的结构、数据摘要和排除注释必须完全一致。"""

    for key in ("tables", "columns", "constraints", "indexes", "data", "excluded_comments"):
        assert before[key] == after[key], key
    assert {
        row["table_name"]: row["comment"] for row in before["comments"]
        if row["table_name"] not in EXPECTED_COMMENTS
    } == {
        row["table_name"]: row["comment"] for row in after["comments"]
        if row["table_name"] not in EXPECTED_COMMENTS
    }
    assert [row for row in before["column_comments"]
            if row["table_name"] not in EXPECTED_COLUMNS] == [
        row for row in after["column_comments"] if row["table_name"] not in EXPECTED_COLUMNS
    ]
