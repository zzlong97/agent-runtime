"""复现首批隔离验证；只引用受信配置路径，不保存或打印凭据。"""

import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg import sql
from pydantic import PostgresDsn

import agent_runtime
from agent_runtime.core.config import Settings
from agent_runtime.core.event_loop import psycopg_compatible_loop_factory
from agent_runtime.runtime.ownership import PostgresRunOwnershipRepository
from agent_runtime.runtime.repository import PostgresRunRepository
from agent_runtime.sessions.repository import PostgresSessionRepository

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
DATABASE = 'mi_dev01_20261030_6d2f8b90'
REGRESSION_SCHEMA = 'mi01_regression_6d2f8b90'
OPERATION = 'mi-dev01-pg-20261030-6d2f8b90'
FILES = (
    'src/agent_runtime/core/config.py',
    'src/agent_runtime/runtime/ownership.py',
    'src/agent_runtime/persistence/guarded_checkpointer.py',
    'tests/runtime/test_ownership.py',
    'tests/runtime/test_ownership_postgres.py',
    '.dev-team/tasks/MI-DEV-01/evidence/运行验证.py',
)


def main():
    os.chdir(ROOT)
    assert Path(agent_runtime.__file__).resolve() == ROOT/'src/agent_runtime/__init__.py'
    report = {
        'operation_key': OPERATION,
        'started_at': datetime.now(timezone.utc).isoformat(),
        'python': sys.version.split()[0],
        'module_file': str(Path(agent_runtime.__file__).resolve()),
        'database': DATABASE,
        'regression_schema': REGRESSION_SCHEMA,
        'packages': {name: importlib.metadata.version(name) for name in ('psycopg','langgraph-checkpoint-postgres','langgraph','pytest')},
        'files': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in FILES},
        'runs': [],
    }
    (EVIDENCE/'候选版本.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.environ.update(RUN_POSTGRES_TESTS='0', RUN_REDIS_TESTS='0', RUN_MI01_POSTGRES_TESTS='0')
    commands = [
        ('单进程Fake回归', ['tests','-m','not postgres and not redis and not bailian']),
        ('租约与同事务真实PG探针', ['tests/runtime/test_ownership.py','tests/runtime/test_ownership_postgres.py']),
        ('增量结构下原单进程PG回归', [
            'tests/runtime/test_run_postgres_integration.py',
            'tests/runtime/test_interrupt_postgres_integration.py',
            'tests/integration/test_checkpoint_recovery_runtime.py',
            'tests/api/test_async_run_postgres_integration.py',
            'tests/graph/test_stage_one_postgres_persistence.py',
        ]),
    ]
    for index,(name,targets) in enumerate(commands):
        if index == 1:
            os.environ['RUN_MI01_POSTGRES_TESTS']='1'
        if index == 2:
            source=Settings(_env_file=os.environ.get('MI01_TEST_SOURCE_ENV'))
            parts=urlsplit(str(source.database_url))
            dedicated=Settings(_env_file=None,database_url=PostgresDsn(urlunsplit((parts.scheme,parts.netloc,'/'+DATABASE,parts.query,''))))
            # 只创建本任务声明的schema；已有对象须具有同一操作标识。
            with psycopg.connect(dedicated.database_connection_string,autocommit=True) as conn:
                assert conn.execute('SELECT current_database()').fetchone()[0]==DATABASE
                row=conn.execute('SELECT obj_description(oid) FROM pg_namespace WHERE nspname=%s',(REGRESSION_SCHEMA,)).fetchone()
                if row is None:
                    conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(REGRESSION_SCHEMA)))
                    conn.execute(sql.SQL('COMMENT ON SCHEMA {} IS {}').format(sql.Identifier(REGRESSION_SCHEMA),sql.Literal(OPERATION)))
                else:
                    assert row[0]==OPERATION
                report['postgres_version']=conn.execute('SHOW server_version').fetchone()[0]
            query=dict(parse_qsl(parts.query));query['options']=f'-c search_path={REGRESSION_SCHEMA},public'
            regression=Settings(_env_file=None,database_url=PostgresDsn(urlunsplit((parts.scheme,parts.netloc,'/'+DATABASE,urlencode(query,quote_via=quote),''))))
            async def initialize():
                await PostgresSessionRepository(regression).setup()
                await PostgresRunRepository(regression).setup()
                repo=PostgresRunOwnershipRepository(regression,contract_hash='a'*64)
                await repo.setup()
                await repo.initialize_contract()
            with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
                runner.run(initialize())
            # 仅当前验证进程的环境；原.env与主Runtime不变，Redis明确不可达降级。
            os.environ.update(DATABASE_URL=str(regression.database_url),RUN_POSTGRES_TESTS='1',RUN_MI01_POSTGRES_TESTS='0',REDIS_URL='redis://127.0.0.1:1/0')
        args=[*targets,'-q','--tb=short','-o','junit_family=xunit1','-o','junit_logging=all',f'--junitxml={EVIDENCE/name}.xml']
        # 每批使用新解释器，避免pytest导入缓存保留上一批的skipif环境条件。
        result=subprocess.run([sys.executable,'-m','pytest',*args],check=False).returncode
        suite=ElementTree.parse(EVIDENCE/f'{name}.xml').getroot().find('testsuite')
        counts={key:int(suite.get(key,'0')) for key in ('tests','failures','errors','skipped')}
        if index and counts['skipped']:
            result=1
        report['runs'].append({'name':name,'args':args,'exit_code':result,'counts':counts})
        report['updated_at']=datetime.now(timezone.utc).isoformat()
        (EVIDENCE/'验证结果.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        if result:
            return result
    return 0


if __name__=='__main__':
    raise SystemExit(main())
