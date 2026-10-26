"""DB-001 一次性核验脚本；默认只读，显式参数才执行已批准的业务 setup。"""

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from agent_runtime.core.config import Settings
from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
from agent_runtime.persistence.database import open_database_connection
from tests.persistence.table_comments_support import (
    assert_comments, assert_unchanged, setup_all, snapshot,
)


async def inspect_target(settings):
    """核对业务数据库身份、活动 Run 和当前连接活动，不导出 SQL 正文。"""

    async with open_database_connection(settings) as connection:
        target = await (await connection.execute("""
            SELECT current_database() AS database, current_user AS role,
                   inet_server_port() AS port, version() AS version
        """)).fetchone()
        assert target["database"] == "agent_runtime", "目标不是已批准的当前业务库"
        active_runs = await (await connection.execute("""
            SELECT count(*) AS count FROM runs WHERE status IN
            ('queued', 'running', 'recovering', 'interrupted', 'cancel_requested')
        """)).fetchone()
        activity = await (await connection.execute("""
            SELECT pid, application_name, client_addr::text AS client_addr,
                   state, backend_start::text AS backend_start,
                   xact_start::text AS transaction_start,
                   state_change::text AS state_change
            FROM pg_stat_activity
            WHERE datname = current_database() AND pid <> pg_backend_pid()
            ORDER BY pid
        """)).fetchall()
        active_summary = await (await connection.execute("""
            SELECT status, count(*) AS count,
                   min(updated_at)::text AS oldest_updated_at,
                   max(updated_at)::text AS newest_updated_at
            FROM runs WHERE status IN
            ('queued', 'running', 'recovering', 'interrupted', 'cancel_requested')
            GROUP BY status ORDER BY status
        """)).fetchall()
        isolated_remaining = await (await connection.execute("""
            SELECT count(*) AS count FROM pg_database
            WHERE datname = 'db001_comments_7f6a83681c634b98a1500416e4f0cc24'
        """)).fetchone()
    return {"target": target, "active_runs": active_runs["count"], "activity": activity,
            "active_summary": active_summary, "isolated_database_remaining": isolated_remaining["count"]}


async def main(apply_current, before_path):
    settings = Settings()
    preflight = await inspect_target(settings)
    before = await snapshot(settings)
    if not apply_current:
        return {**preflight, "snapshot": before}
    assert all(row["state"] == "idle" for row in preflight["activity"]), "存在非空闲连接，等待空闲窗口"
    assert all(row["transaction_start"] is None for row in preflight["activity"]), "存在活动事务，等待空闲窗口"
    expected = json.loads(Path(before_path).read_text(encoding="utf-8"))
    assert expected["target"] == preflight["target"], "目标发生变化"
    assert expected["snapshot"] == before, "执行前快照已经变化，必须重新核验"
    await setup_all(settings)
    after = await snapshot(settings)
    assert_comments(after)
    assert_unchanged(before, after)
    return {**preflight, "snapshot": after,
            "result": "九表精确注释已更新；结构、业务数据及排除对象注释无变化"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DB-001 数据库元数据核验")
    parser.add_argument("--apply-current", action="store_true")
    parser.add_argument("--before")
    args = parser.parse_args()
    assert not args.apply_current or args.before, "实际应用必须指定已保存的前快照"
    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        result = runner.run(main(args.apply_current, args.before))
    print("DB001_CURRENT_RESULT=" + json.dumps(result, ensure_ascii=False))
