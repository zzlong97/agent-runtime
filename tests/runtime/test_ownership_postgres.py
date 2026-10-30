"""仅在专用PG测试库执行的租约、元数据与同事务Saver真实探针。"""

import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from time import perf_counter
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb, JsonbDumper, set_json_dumps
from pydantic import PostgresDsn

from agent_runtime.core.config import Settings
from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
from agent_runtime.runtime.repository import PostgresRunRepository
from agent_runtime.sessions.repository import PostgresSessionRepository

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    os.environ.get("RUN_MI01_POSTGRES_TESTS") != "1",
    reason="须显式授权MI-DEV-01专用PG探针",
)]
DATABASE = "mi_dev01_20261030_6d2f8b90"
CONTRACT = "a" * 64


def run(coro):
    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        return runner.run(coro)


@pytest.fixture(scope="session")
def isolated_database():
    # 只替换数据库名；受信配置在内存中使用，不能把连接信息写入证据。
    source = Settings(_env_file=os.environ.get("MI01_TEST_SOURCE_ENV"))
    admin = make_conninfo(source.database_connection_string, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DATABASE,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
    parts = urlsplit(str(source.database_url))
    url = urlunsplit((parts.scheme, parts.netloc, "/" + DATABASE, parts.query, ""))
    configured = Settings(_env_file=None, database_url=PostgresDsn(url))
    with psycopg.connect(configured.database_connection_string, autocommit=True) as conn:
        name, version = conn.execute("SELECT current_database(), current_setting('server_version_num')::int").fetchone()
        assert name == DATABASE and 170000 <= version < 180000
    return configured


@pytest.fixture
def database(isolated_database, request):
    schema = "mi01_" + uuid4().hex
    with psycopg.connect(isolated_database.database_connection_string, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    parts = urlsplit(str(isolated_database.database_url))
    query = dict(parse_qsl(parts.query))
    query["options"] = f"-c search_path={schema},public"
    settings = Settings(_env_file=None, database_url=PostgresDsn(urlunsplit((
        parts.scheme, parts.netloc, parts.path, urlencode(query, quote_via=quote), "",
    ))))
    request.node.user_properties.extend([("database", DATABASE), ("schema", schema)])
    return settings


@asynccontextmanager
async def connection(settings):
    conn = await psycopg.AsyncConnection.connect(
        settings.database_connection_string, autocommit=True, row_factory=dict_row,
    )
    try:
        yield conn
    finally:
        await conn.close()


async def legacy(settings):
    await PostgresSessionRepository(settings).setup()
    await PostgresRunRepository(settings).setup()


async def prepared(settings):
    from agent_runtime.runtime.ownership import PostgresRunOwnershipRepository
    await legacy(settings)
    repository = PostgresRunOwnershipRepository(settings, contract_hash=CONTRACT)
    await repository.setup()
    await repository.initialize_contract()
    return repository


async def seed(settings, status="queued"):
    session_id, run_id = uuid4(), uuid4()
    async with connection(settings) as conn:
        await conn.execute("INSERT INTO sessions (session_id,user_id,title,created_at,updated_at) VALUES (%s,'mi-test','合成会话',clock_timestamp(),clock_timestamp())", (session_id,))
        await conn.execute("""INSERT INTO runs
            (run_id,request_id,session_id,thread_id,run_type,input_message_id,response_message_id,
             input_payload,request_fingerprint,status,created_at,updated_at)
            VALUES (%s,%s,%s,%s,'normal',%s,%s,'{}',%s,%s,clock_timestamp(),clock_timestamp())""",
            (run_id, uuid4(), session_id, str(session_id), uuid4(), uuid4(), "f" * 64, status))
    return session_id, run_id


async def fetch(settings, query, params=()):
    async with connection(settings) as conn:
        return await (await conn.execute(query, params)).fetchall()


async def metadata(settings):
    return await fetch(settings, """SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod) AS type,
        col_description(c.oid,a.attnum) AS comment FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid WHERE n.nspname=current_schema()
        AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum""")


def test_new_old_and_repeated_schema_preserve_data_structure_acl_and_comments(database):
    async def scenario():
        from agent_runtime.runtime.ownership import PostgresRunOwnershipRepository
        await legacy(database)
        session_id, run_id = await seed(database)
        async with connection(database) as conn:
            saver = AsyncPostgresSaver(conn)
            await saver.setup()
            await conn.execute("COMMENT ON TABLE checkpoints IS '保留既有框架注释'")
            await conn.execute("COMMENT ON COLUMN checkpoint_blobs.blob IS '保留既有框架字段注释'")
            await conn.execute('GRANT SELECT ON runs TO PUBLIC')
        before_columns = await metadata(database)
        before_data = await fetch(database, "SELECT to_jsonb(r) AS row FROM runs r")
        before_constraints = await fetch(database, "SELECT conname,pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conrelid IN ('runs'::regclass,'sessions'::regclass) ORDER BY conname")
        before_indexes = await fetch(database, "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=current_schema() ORDER BY indexname")
        before_acl = await fetch(database, "SELECT relname,relacl,relowner FROM pg_class WHERE oid IN ('runs'::regclass,'sessions'::regclass)")
        repo = PostgresRunOwnershipRepository(database, contract_hash=CONTRACT)
        await repo.setup()
        after = await metadata(database)
        added = {(r['relname'], r['attname']): r for r in after if (r['relname'],r['attname']) not in {(x['relname'],x['attname']) for x in before_columns}}
        expected = {('runs','owner_instance_id'):'uuid',('runs','execution_epoch'):'bigint',('runs','lease_expires_at'):'timestamp with time zone',('sessions','deletion_requested_at'):'timestamp with time zone',('runtime_cluster_config','singleton_id'):'integer',('runtime_cluster_config','contract_hash'):'character(64)'}
        assert {key: value['type'] for key,value in added.items()} == expected
        assert all(r['comment'] and any('\u4e00'<=c<='\u9fff' for c in r['comment']) for r in added.values())
        assert all(r in after for r in before_columns)
        assert await fetch(database, "SELECT to_jsonb(r)-'owner_instance_id'-'execution_epoch'-'lease_expires_at' AS row FROM runs r") == before_data
        assert await fetch(database, "SELECT conname,pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conrelid IN ('runs'::regclass,'sessions'::regclass) AND conname NOT IN ('runs_execution_epoch_nonnegative','runs_owner_expiry_pair') ORDER BY conname") == before_constraints
        assert await fetch(database, "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=current_schema() AND tablename!='runtime_cluster_config' ORDER BY indexname") == before_indexes
        assert await fetch(database, "SELECT relname,relacl,relowner FROM pg_class WHERE oid IN ('runs'::regclass,'sessions'::regclass)") == before_acl
        assert (await fetch(database, "SELECT obj_description('checkpoints'::regclass) AS comment"))[0]['comment'] == '保留既有框架注释'
        assert (await fetch(database, "SELECT obj_description('runtime_cluster_config'::regclass) AS comment"))[0]['comment']
        await repo.setup()
        assert await metadata(database) == after
        column = (await fetch(database, "SELECT is_nullable,column_default FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='runs' AND column_name='execution_epoch'"))[0]
        assert column == {'is_nullable':'NO','column_default':'0'}
        async with connection(database) as conn:
            for statement in (
                "UPDATE runs SET execution_epoch=-1 WHERE run_id=%s",
                "UPDATE runs SET owner_instance_id=gen_random_uuid() WHERE run_id=%s",
            ):
                with pytest.raises(psycopg.errors.CheckViolation):
                    await conn.execute(statement, (run_id,))
            with pytest.raises(psycopg.errors.CheckViolation):
                await conn.execute("INSERT INTO runtime_cluster_config VALUES (2,%s)", (CONTRACT,))
        print(json.dumps({'new_columns':6,'legacy_rows_preserved':1,'repeat_setup':'identical','constraints':'enforced','acl':'preserved','framework_comment':'preserved'}))
    run(scenario())


def test_fresh_schema_has_all_chinese_metadata_before_any_business_row(database):
    async def scenario():
        repo = await prepared(database)
        rows = await metadata(database)
        assert {r['relname'] for r in rows} == {'runs','sessions','run_interrupts','runtime_cluster_config'}
        assert all(r['comment'] and any('\u4e00' <= char <= '\u9fff' for char in r['comment']) for r in rows)
        table_comments = await fetch(database, "SELECT obj_description(c.oid) AS comment FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema() AND c.relkind='r'")
        assert all(r['comment'] and any('\u4e00' <= char <= '\u9fff' for char in r['comment']) for r in table_comments)
        assert await fetch(database,'SELECT count(*) AS n FROM sessions') == [{'n':0}]
        assert await fetch(database,'SELECT count(*) AS n FROM runs') == [{'n':0}]
        await repo.setup()
        assert await metadata(database) == rows
    run(scenario())


def test_schema_initialization_failure_rolls_back_all_additions(database, monkeypatch):
    async def scenario():
        import agent_runtime.runtime.ownership as module
        await legacy(database)
        await seed(database)
        before = await metadata(database)
        monkeypatch.setattr(module, "_SCHEMA_STATEMENTS", (*module._SCHEMA_STATEMENTS, "SELECT missing_mi01_column"))
        with pytest.raises(psycopg.errors.UndefinedColumn):
            await module.PostgresRunOwnershipRepository(database, contract_hash=CONTRACT).setup()
        assert await metadata(database) == before
    run(scenario())


def test_contract_is_explicit_immutable_and_checked_for_claim_renew_and_guard(database):
    async def scenario():
        from agent_runtime.runtime.ownership import DeploymentContractMismatch, PostgresRunOwnershipRepository
        await legacy(database)
        repo = PostgresRunOwnershipRepository(database, contract_hash=CONTRACT)
        await repo.setup()
        _, run_id = await seed(database)
        with pytest.raises(DeploymentContractMismatch):
            await repo.claim(run_id, uuid4())
        await repo.initialize_contract()
        await repo.initialize_contract()
        bad = PostgresRunOwnershipRepository(database, contract_hash='b'*64)
        with pytest.raises(DeploymentContractMismatch):
            await bad.initialize_contract()
        with pytest.raises(DeploymentContractMismatch):
            await bad.claim(run_id, uuid4())
        token = await repo.claim(run_id, uuid4())
        with pytest.raises(DeploymentContractMismatch):
            await bad.renew(token)
        with pytest.raises(DeploymentContractMismatch):
            async with bad.guard(token):
                pytest.fail('契约不符不能进入写事务')
        assert (await fetch(database, "SELECT contract_hash FROM runtime_cluster_config"))[0]['contract_hash'] == CONTRACT
        for invalid in ('', 'A'*64, 'x'*64):
            with pytest.raises(ValueError):
                PostgresRunOwnershipRepository(database, contract_hash=invalid)
    run(scenario())


def test_competing_claim_takeover_renew_and_explicit_reconciliation(database):
    async def scenario():
        from agent_runtime.runtime.ownership import RunOwnershipLost
        repo = await prepared(database)
        session_id, run_id = await seed(database)
        results = await asyncio.gather(*(repo.claim(run_id, uuid4(), lease_seconds=.18) for _ in range(2)))
        assert sum(token is not None for token in results) == 1
        first = next(token for token in results if token)
        await repo.renew(first, lease_seconds=.2)
        await asyncio.sleep(.24)
        with pytest.raises(RunOwnershipLost):
            await repo.renew(first)
        second = await repo.claim(run_id, uuid4())
        assert second.execution_epoch == first.execution_epoch + 1
        with pytest.raises(RunOwnershipLost):
            await repo.release(first)
        await repo.release(second)
        rows = await fetch(database, 'SELECT owner_instance_id,lease_expires_at,execution_epoch,recovery_attempts,status FROM runs WHERE run_id=%s',(run_id,))
        assert rows[0] == dict(owner_instance_id=None, lease_expires_at=None, execution_epoch=2, recovery_attempts=0,status='queued')
        async with connection(database) as conn:
            await conn.execute("UPDATE runs SET status='running' WHERE run_id=%s",(run_id,))
        resumed = await repo.claim(run_id, uuid4())
        assert resumed
        await repo.release(resumed)
        async with connection(database) as conn:
            await conn.execute("UPDATE runs SET status='interrupted' WHERE run_id=%s",(run_id,))
        assert await repo.claim(run_id, uuid4()) is None
        reconciler = await repo.claim_for_reconciliation(run_id, uuid4())
        assert reconciler
        await repo.release(reconciler)
        async with connection(database) as conn:
            await conn.execute("UPDATE sessions SET deletion_requested_at=clock_timestamp() WHERE session_id=%s",(session_id,))
            await conn.execute("UPDATE runs SET status='queued' WHERE run_id=%s",(run_id,))
        assert await repo.claim(run_id, uuid4()) is None
        async with connection(database) as conn:
            await conn.execute("UPDATE runs SET status='cancel_requested' WHERE run_id=%s",(run_id,))
        assert await repo.claim(run_id, uuid4())
    run(scenario())


def test_renew_checks_database_clock_after_waiting_for_run_lock(database):
    async def scenario():
        from agent_runtime.runtime.ownership import RunOwnershipLost
        repo = await prepared(database)
        _, run_id = await seed(database)
        token = await repo.claim(run_id, uuid4(), lease_seconds=.25)
        async with connection(database) as blocker:
            async with blocker.transaction():
                await blocker.execute('SELECT run_id FROM runs WHERE run_id=%s FOR UPDATE',(run_id,))
                task = asyncio.create_task(repo.renew(token))
                await asyncio.sleep(.32)
                assert not task.done()
            with pytest.raises(RunOwnershipLost):
                await task
        assert (await fetch(database,'SELECT execution_epoch FROM runs'))[0]['execution_epoch'] == 1
    run(scenario())


def test_takeover_cannot_advance_epoch_until_old_short_transaction_finishes(database):
    async def scenario():
        from agent_runtime.runtime.ownership import RunOwnershipLost
        repo=await prepared(database)
        _,run_id=await seed(database)
        old=await repo.claim(run_id,uuid4(),lease_seconds=.15)
        async with repo.guard(old) as conn:
            await conn.execute('CREATE TABLE mi01_old_transaction(value integer)')
            await conn.execute('INSERT INTO mi01_old_transaction VALUES (1)')
            await asyncio.sleep(.2)
            assert await repo.claim(run_id,uuid4()) is None
            assert (await fetch(database,'SELECT execution_epoch FROM runs'))[0]['execution_epoch']==1
        new=await repo.claim(run_id,uuid4())
        assert new.execution_epoch==2
        with pytest.raises(RunOwnershipLost):
            async with repo.guard(old):
                pytest.fail('新代次提交后旧事务不能再启动')
        assert await fetch(database,'SELECT value FROM mi01_old_transaction')==[{'value':1}]
    run(scenario())


def checkpoint():
    value = empty_checkpoint()
    value['channel_values'] = {'business': {'synthetic': 1}}
    value['channel_versions'] = {'business': '1'}
    return value


async def saver_for(settings, repo):
    from agent_runtime.persistence.guarded_checkpointer import GuardedAsyncPostgresSaver
    conn = await psycopg.AsyncConnection.connect(settings.database_connection_string,autocommit=True)
    saver = GuardedAsyncPostgresSaver(conn, ownership=repo)
    await saver.setup()
    return conn, saver


@pytest.mark.parametrize('refusal',['missing','expired','stale'])
def test_guarded_saver_reads_thread_isolation_and_refuses_both_writes(database, refusal):
    async def scenario():
        from agent_runtime.runtime.ownership import RunOwnershipLost, bind_execution_token
        repo = await prepared(database)
        _, run_id = await seed(database)
        token = await repo.claim(run_id,uuid4(),lease_seconds=.3 if refusal!='missing' else 15)
        conn,saver = await saver_for(database,repo)
        config={'configurable':{'thread_id':'parent:synthetic','checkpoint_ns':''}}
        try:
            with bind_execution_token(token):
                result=await saver.aput(config,checkpoint(),{'source':'input','step':0,'parents':{}},{'business':'1'})
                await saver.aput_writes(result,[('business',{'synthetic':2})],'task-1')
            assert await saver.aget_tuple(config)
            assert await saver.aget_tuple({'configurable':{'thread_id':'child:synthetic','checkpoint_ns':''}}) is None
            if refusal!='missing':
                await asyncio.sleep(.34)
                if refusal=='stale':
                    assert (await repo.claim(run_id,uuid4())).execution_epoch == 2
            before=await counts(database)
            for operation in (
                lambda:saver.aput(config,checkpoint(),{'source':'input','step':1,'parents':{}},{'business':'2'}),
                lambda:saver.aput_writes(result,[('business',{'synthetic':3})],'task-2'),
            ):
                if refusal=='missing':
                    with pytest.raises(RunOwnershipLost):
                        await operation()
                else:
                    with bind_execution_token(token),pytest.raises(RunOwnershipLost):
                        await operation()
            assert await counts(database)==before
            assert await saver.aget_tuple(config)
        finally:
            await conn.close()
    run(scenario())


async def counts(settings):
    result={}
    for table in ('checkpoints','checkpoint_blobs','checkpoint_writes'):
        result[table]=(await fetch(settings,f'SELECT count(*) AS n FROM {table}'))[0]['n']
    return result


def test_pipeline_failures_rollback_checkpoint_blobs_and_writes(database):
    async def scenario():
        from agent_runtime.runtime.ownership import bind_execution_token
        repo=await prepared(database)
        _,run_id=await seed(database)
        token=await repo.claim(run_id,uuid4())
        conn,saver=await saver_for(database,repo)
        config={'configurable':{'thread_id':'rollback','checkpoint_ns':''}}
        try:
            before=await counts(database)
            original=saver.UPSERT_CHECKPOINTS_SQL
            saver.UPSERT_CHECKPOINTS_SQL=original.replace('parent_checkpoint_id, checkpoint, metadata','parent_checkpoint_id, checkpoint, missing_checkpoint_column')
            with bind_execution_token(token),pytest.raises(psycopg.errors.UndefinedColumn):
                await saver.aput(config,checkpoint(),{'source':'input','step':0,'parents':{}},{'business':'1'})
            assert await counts(database)==before
            saver.UPSERT_CHECKPOINTS_SQL=original
            with bind_execution_token(token):
                result=await saver.aput(config,checkpoint(),{'source':'input','step':0,'parents':{}},{'business':'1'})
            before=await counts(database)
            # 第二行真实触发数据库约束，第一行也不能留下；不是仅抛Python异常。
            async with connection(database) as observer:
                await observer.execute("ALTER TABLE checkpoint_writes ADD CONSTRAINT mi01_write_failure CHECK (task_id != 'fail-task' OR idx=0)")
            with bind_execution_token(token),pytest.raises(psycopg.errors.CheckViolation):
                await saver.aput_writes(result,[('one',1),('two',2)],'fail-task')
            assert await counts(database)==before
            with bind_execution_token(token):
                await saver.aput_writes(result,[('one',1)],'normal-task')
            assert (await counts(database))['checkpoint_writes']==1
        finally:
            await conn.close()
    run(scenario())


def test_saver_actual_sql_and_guard_share_connection_and_hold_run_lock(database):
    async def scenario():
        from agent_runtime.runtime.ownership import PostgresRunOwnershipRepository,bind_execution_token
        entered,proceed=asyncio.Event(),asyncio.Event()
        pids=[]
        class ObservedRepository(PostgresRunOwnershipRepository):
            @asynccontextmanager
            async def guard(self,token):
                async with super().guard(token) as conn:
                    pids.append((await (await conn.execute('SELECT pg_backend_pid() AS pid')).fetchone())['pid'])
                    entered.set()
                    await proceed.wait()
                    yield conn
        await prepared(database)
        repo=ObservedRepository(database,contract_hash=CONTRACT)
        _,run_id=await seed(database)
        token=await repo.claim(run_id,uuid4())
        conn,saver=await saver_for(database,repo)
        config={'configurable':{'thread_id':'same-connection','checkpoint_ns':''}}
        async with connection(database) as observer:
            await observer.execute('CREATE TABLE mi01_checkpoint_connection(pid integer)')
            await observer.execute("""CREATE FUNCTION mi01_record_pid() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN INSERT INTO mi01_checkpoint_connection VALUES (pg_backend_pid()); RETURN NEW; END $$""")
            await observer.execute('CREATE TRIGGER mi01_checkpoint_pid AFTER INSERT ON checkpoints FOR EACH ROW EXECUTE FUNCTION mi01_record_pid()')
        async def write():
            with bind_execution_token(token):
                await saver.aput(config,checkpoint(),{'source':'input','step':0,'parents':{}},{'business':'1'})
        task=asyncio.create_task(write())
        try:
            await asyncio.wait_for(entered.wait(),2)
            async with connection(database) as observer:
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    await observer.execute('SELECT run_id FROM runs WHERE run_id=%s FOR UPDATE NOWAIT',(run_id,))
                assert (await (await observer.execute('SELECT count(*) AS n FROM checkpoints')).fetchone())['n']==0
            proceed.set()
            await task
            assert [row['pid'] for row in await fetch(database,'SELECT pid FROM mi01_checkpoint_connection')]==pids
            async with connection(database) as observer:
                await observer.execute('SELECT run_id FROM runs WHERE run_id=%s FOR UPDATE NOWAIT',(run_id,))
        finally:
            proceed.set()
            await asyncio.gather(task,return_exceptions=True)
            await conn.close()
    run(scenario())


@pytest.mark.parametrize('mode',['continuous','lock','idle'])
def test_whole_transaction_budget_rolls_back_and_replaces_failed_connection(database,mode):
    async def scenario():
        from agent_runtime.runtime.ownership import PostgresRunOwnershipRepository
        await prepared(database)
        _,run_id=await seed(database)
        token=await PostgresRunOwnershipRepository(database,contract_hash=CONTRACT).claim(run_id,uuid4())
        limited=database.model_copy(update={'run_transaction_timeout_seconds':.3})
        repo=PostgresRunOwnershipRepository(limited,contract_hash=CONTRACT)
        async with connection(database) as observer:
            await observer.execute('CREATE TABLE mi01_budget(value integer)')
            await observer.execute('INSERT INTO mi01_budget VALUES (0)')
            blocker=observer.transaction()
            if mode=='lock':
                await blocker.__aenter__()
                await observer.execute('SELECT * FROM mi01_budget FOR UPDATE')
            started=perf_counter()
            failed=None
            try:
                with pytest.raises(psycopg.Error) as error:
                    async with repo.guard(token) as guarded:
                        failed=guarded
                        timeout=(await (await guarded.execute("SELECT current_setting('transaction_timeout') AS budget")).fetchone())['budget']
                        assert timeout=='300ms'
                        await guarded.execute('INSERT INTO mi01_budget VALUES (1)')
                        if mode=='continuous':
                            for _ in range(20):
                                await guarded.execute('SELECT pg_sleep(.04)')
                        elif mode=='lock':
                            await guarded.execute('UPDATE mi01_budget SET value=2 WHERE value=0')
                        else:
                            await asyncio.sleep(.5)
                            await guarded.execute('SELECT 1')
                elapsed=perf_counter()-started
                assert elapsed<1.5
                assert failed.closed
                if mode=='continuous':
                    assert isinstance(error.value,psycopg.errors.TransactionTimeout)
                print(json.dumps({'mode':mode,'elapsed_ms':round(elapsed*1000),'error_type':type(error.value).__name__,'failed_connection_closed':failed.closed}))
            finally:
                if mode=='lock':
                    await blocker.__aexit__(None,None,None)
        assert await fetch(database,'SELECT value FROM mi01_budget')==[{'value':0}]
        async with repo.guard(token) as recovered:
            assert recovered is not failed
            await recovered.execute('INSERT INTO mi01_budget VALUES (3)')
        assert await fetch(database,'SELECT value FROM mi01_budget ORDER BY value')==[{'value':0},{'value':3}]
    run(scenario())


@pytest.mark.parametrize('phase',['blob','checkpoint_json','metadata_json','writes'])
def test_slow_serialization_has_no_run_lock_and_does_not_consume_transaction_budget(database,phase):
    async def scenario():
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from agent_runtime.persistence.guarded_checkpointer import GuardedAsyncPostgresSaver
        from agent_runtime.runtime.ownership import PostgresRunOwnershipRepository,bind_execution_token
        await prepared(database)
        _,run_id=await seed(database)
        repo=PostgresRunOwnershipRepository(database.model_copy(update={'run_transaction_timeout_seconds':.3}),contract_hash=CONTRACT)
        token=await repo.claim(run_id,uuid4())
        lock_free=[]
        armed=False
        def observe_and_wait():
            # 在真实序列化调用内由独立连接观察；JSON编码即使占住事件循环也能检测。
            with psycopg.connect(database.database_connection_string,autocommit=True) as observer:
                try:
                    with observer.transaction():
                        observer.execute('SELECT run_id FROM runs WHERE run_id=%s FOR UPDATE NOWAIT',(run_id,))
                    lock_free.append(True)
                except psycopg.errors.LockNotAvailable:
                    lock_free.append(False)
            time.sleep(.45)
        class SlowSerde(JsonPlusSerializer):
            def dumps_typed(self,value):
                if armed and phase in ('blob','writes'):
                    observe_and_wait()
                return super().dumps_typed(value)
        original_dumps=JsonbDumper(Jsonb).dumps
        def slow_json(value):
            if armed and isinstance(value,dict) and (
                (phase=='checkpoint_json' and 'channel_values' in value) or
                (phase=='metadata_json' and value.get('source')=='input')
            ):
                observe_and_wait()
            return original_dumps(value)
        conn=await psycopg.AsyncConnection.connect(database.database_connection_string,autocommit=True)
        saver=GuardedAsyncPostgresSaver(conn,ownership=repo,serde=SlowSerde())
        await saver.setup()
        config={'configurable':{'thread_id':'slow:'+phase,'checkpoint_ns':''}}
        try:
            if phase=='writes':
                with bind_execution_token(token):
                    config=await saver.aput(config,checkpoint(),{'source':'input','step':0,'parents':{}},{'business':'1'})
            armed=True
            set_json_dumps(slow_json)
            error=None
            started=perf_counter()
            try:
                with bind_execution_token(token):
                    if phase=='writes':
                        await saver.aput_writes(config,[('business',{'synthetic':2})],'slow-task')
                    else:
                        await saver.aput(config,checkpoint(),{'source':'input','step':0,'parents':{}},{'business':'1'})
            except psycopg.Error as exc:
                error=type(exc).__name__
            assert lock_free==[True], '序列化不得持有Run行锁'
            assert error is None, f'450ms序列化不应耗尽300ms事务预算：{error}'
            assert perf_counter()-started>=.45
            assert await saver.aget_tuple(config)
            print(json.dumps({'phase':phase,'serialization_delay_ms':450,'transaction_budget_ms':300,'run_lock_free_during_serialization':lock_free,'success':True}))
        finally:
            set_json_dumps(original_dumps)
            await conn.close()
    run(scenario())


@pytest.mark.parametrize('method',['aput','aput_writes'])
def test_missing_token_is_rejected_before_serialization(database,method):
    async def scenario():
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from agent_runtime.persistence.guarded_checkpointer import GuardedAsyncPostgresSaver
        from agent_runtime.runtime.ownership import RunOwnershipLost
        repo=await prepared(database)
        calls=[]
        class ObservedSerde(JsonPlusSerializer):
            def dumps_typed(self,value):
                calls.append(True)
                return super().dumps_typed(value)
        async with connection(database) as conn:
            saver=GuardedAsyncPostgresSaver(conn,ownership=repo,serde=ObservedSerde())
            await saver.setup()
            config={'configurable':{'thread_id':'missing-token','checkpoint_ns':'','checkpoint_id':'synthetic'}}
            with pytest.raises(RunOwnershipLost):
                if method=='aput':
                    await saver.aput(config,checkpoint(),{'source':'input','step':0,'parents':{}},{'business':'1'})
                else:
                    await saver.aput_writes(config,[('business',{'synthetic':1})],'task')
            assert calls==[]
            assert await counts(database)=={'checkpoints':0,'checkpoint_blobs':0,'checkpoint_writes':0}
    run(scenario())


@pytest.mark.parametrize('change',['expired','takeover','contract'])
@pytest.mark.parametrize('method',['aput','aput_writes'])
def test_execution_rights_are_rechecked_after_serialization(database,change,method):
    async def scenario():
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from agent_runtime.persistence.guarded_checkpointer import GuardedAsyncPostgresSaver
        from agent_runtime.runtime.ownership import DeploymentContractMismatch,RunOwnershipLost,bind_execution_token
        repo=await prepared(database)
        _,run_id=await seed(database)
        token=await repo.claim(run_id,uuid4())
        started,proceed=threading.Event(),threading.Event()
        armed=False
        class PausedSerde(JsonPlusSerializer):
            def dumps_typed(self,value):
                if armed:
                    started.set()
                    assert proceed.wait(3), '测试序列化等待超过保护时间'
                return super().dumps_typed(value)
        async with connection(database) as conn:
            saver=GuardedAsyncPostgresSaver(conn,ownership=repo,serde=PausedSerde())
            await saver.setup()
            config={'configurable':{'thread_id':'rights:'+change+method,'checkpoint_ns':''}}
            if method=='aput_writes':
                with bind_execution_token(token):
                    config=await saver.aput(config,checkpoint(),{'source':'input','step':0,'parents':{}},{'business':'1'})
            before=await counts(database)
            if change!='contract':
                await repo.renew(token,lease_seconds=.2)
            armed=True
            async def write():
                with bind_execution_token(token):
                    if method=='aput_writes':
                        return await saver.aput_writes(config,[('business',{'synthetic':2})],'paused-task')
                    return await saver.aput(config,checkpoint(),{'source':'input','step':0,'parents':{}},{'business':'1'})
            task=asyncio.create_task(write())
            try:
                assert await asyncio.to_thread(started.wait,2)
                if change in ('expired','takeover'):
                    await asyncio.sleep(.24)
                    if change=='takeover':
                        new=await repo.claim(run_id,uuid4())
                        assert new is not None and new.execution_epoch==token.execution_epoch+1
                else:
                    async with connection(database) as admin:
                        await admin.execute("SET statement_timeout='1s'")
                        await admin.execute("UPDATE runtime_cluster_config SET contract_hash=%s WHERE singleton_id=1",('b'*64,))
                proceed.set()
                with pytest.raises(DeploymentContractMismatch if change=='contract' else RunOwnershipLost):
                    await task
                assert await counts(database)==before
            finally:
                proceed.set()
                await asyncio.gather(task,return_exceptions=True)
    run(scenario())


def test_prepared_checkpoint_keeps_primitive_blob_metadata_and_parent_link(database):
    async def scenario():
        from copy import deepcopy
        from agent_runtime.runtime.ownership import bind_execution_token
        repo=await prepared(database)
        _,run_id=await seed(database)
        token=await repo.claim(run_id,uuid4())
        conn,saver=await saver_for(database,repo)
        original=empty_checkpoint()
        original['channel_values']={'number':1,'flag':True,'empty':None,'text':'合成文本','object':{'nested':[1,2]}}
        original['channel_versions']={key:'1' for key in original['channel_values']}
        unchanged=deepcopy(original)
        config={'configurable':{'thread_id':'representation','checkpoint_ns':'child'}}
        try:
            with bind_execution_token(token):
                first=await saver.aput(config,original,{'source':'input','step':0,'parents':{}},original['channel_versions'])
                following=deepcopy(original)
                following['id']=str(uuid4())
                second=await saver.aput(first,following,{'source':'loop','step':1,'parents':{}},{})
            saved=await saver.aget_tuple(second)
            assert saved.checkpoint['channel_values']==original['channel_values']
            assert saved.metadata['source']=='loop' and saved.metadata['step']==1
            assert saved.parent_config==first
            assert original==unchanged
            assert await saver.aget_tuple({'configurable':{'thread_id':'representation','checkpoint_ns':'parent'}}) is None
        finally:
            await conn.close()
    run(scenario())
