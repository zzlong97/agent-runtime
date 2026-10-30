"""仅复核MI01-R01及受影响Saver边界；保留首批和审核旧证据。"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree

import agent_runtime

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
FILES = (
    'src/agent_runtime/core/config.py',
    'src/agent_runtime/runtime/ownership.py',
    'src/agent_runtime/persistence/guarded_checkpointer.py',
    'tests/runtime/test_ownership.py',
    'tests/runtime/test_ownership_postgres.py',
    'tests/persistence/test_checkpointers.py',
    '.dev-team/tasks/MI-DEV-01/evidence/R01-运行验证.py',
)


def main():
    os.chdir(ROOT)
    assert Path(agent_runtime.__file__).resolve() == ROOT/'src/agent_runtime/__init__.py'
    report = {
        'issue': 'MI01-R01',
        'operation_key': 'sha256:238de6403425ca04f3a46f4fe76374f473c522f76d0a489ac68b2abfa0be158f',
        'started_at': datetime.now(timezone.utc).isoformat(),
        'module_file': str(Path(agent_runtime.__file__).resolve()),
        'database': 'mi_dev01_20261030_6d2f8b90',
        'files': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in FILES},
    }
    (EVIDENCE/'R01-候选版本.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.environ.update(RUN_POSTGRES_TESTS='0',RUN_REDIS_TESTS='0',RUN_MI01_POSTGRES_TESTS='1')
    args=[
        'tests/runtime/test_ownership.py','tests/runtime/test_ownership_postgres.py',
        'tests/persistence/test_checkpointers.py','-q','--tb=short',
        '-o','junit_family=xunit1','-o','junit_logging=all',
        f'--junitxml={EVIDENCE}/R01-完整原语与Saver回归.xml',
    ]
    report['args']=args
    report['exit_code']=subprocess.run([sys.executable,'-m','pytest',*args],check=False).returncode
    suite=ElementTree.parse(EVIDENCE/'R01-完整原语与Saver回归.xml').getroot().find('testsuite')
    report['counts']={key:int(suite.get(key,'0')) for key in ('tests','failures','errors','skipped')}
    report['finished_at']=datetime.now(timezone.utc).isoformat()
    (EVIDENCE/'R01-验证结果.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    assert report['counts']=={'tests':31,'failures':0,'errors':0,'skipped':0}
    return report['exit_code']


if __name__=='__main__':
    raise SystemExit(main())
