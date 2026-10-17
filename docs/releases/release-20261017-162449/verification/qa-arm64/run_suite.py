#!/usr/bin/env python3
"""Run baked-image tests with regular CPython/GIL/native-binary checks before/after plugin loading."""
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import sys
import sysconfig
import time
import traceback

def require_runtime():
    import psycopg
    from psycopg import pq
    state = {
        'implementation': sys.implementation.name,
        'python_version': list(sys.version_info[:3]),
        'python': sys.version,
        'free_threaded_build': bool(sysconfig.get_config_var('Py_GIL_DISABLED')),
        'gil_enabled': sys._is_gil_enabled(),
        'jit_available': sys._jit.is_available(),
        'jit_enabled': sys._jit.is_enabled(),
        'psycopg_impl': pq.__impl__,
        'psycopg_version': psycopg.__version__,
        'libpq_version': pq.version(),
    }
    assert state['implementation'] == 'cpython' and state['python_version'] == [3, 15, 0], state
    assert not state['free_threaded_build'] and state['gil_enabled'] is True, state
    assert not state['jit_available'] and not state['jit_enabled'], state
    assert state['psycopg_impl'] == 'binary', state
    return state

OUTPUT = Path('/evidence')
REPORT = OUTPUT/'pytest-runtime-summary.json'
report = {'cwd':str(Path.cwd()),'argv':sys.argv,'checkpoints':[]}

SOURCE_MANIFEST = Path('/runner/expected-source-manifest.json')
EXPECTED = {name: record['sha256'] for name, record in json.loads(SOURCE_MANIFEST.read_text()).items()}
report['frozen_source_manifest_sha256'] = hashlib.sha256(SOURCE_MANIFEST.read_bytes()).hexdigest()
report['expected_source_files'] = len(EXPECTED)

def source_hashes():
    root=Path('/app')
    assert not (root/'.env').exists(), 'No real dotenv is permitted in this image'
    for name in EXPECTED:
        assert not Path(name).is_absolute() and '..' not in Path(name).parts
    return {name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in EXPECTED}

def checkpoint(stage):
    report['checkpoints'].append({'stage':stage,**require_runtime()})

class RuntimeGuard:
    def pytest_sessionstart(self, session):
        checkpoint('pytest_plugins_loaded_before_tests')

    def pytest_terminal_summary(self, terminalreporter):
        report['test_outcomes']={kind:len(items) for kind,items in terminalreporter.stats.items()}
        report['skips']=[{'nodeid':item.nodeid,'reason':str(item.longrepr)} for item in terminalreporter.stats.get('skipped',[])]

exit_code=2
try:
    assert Path.cwd()==Path('/app'), 'Run only from actual backend /app'
    checkpoint('before_pytest_import')
    report['packages']={d.metadata['Name']:d.version for d in importlib.metadata.distributions()}
    report['optional_minijinja_installed']=importlib.util.find_spec('minijinja') is not None
    report['source_hashes_before']=source_hashes()
    assert report['source_hashes_before']==EXPECTED, 'Baked source differs from frozen manifest'
    import pytest
    checkpoint('after_pytest_import')
    arguments=['-q','-ra','--junitxml=/evidence/pytest-junit.xml','--basetemp=/tmp/altt-pytest',
               '-o','cache_dir=/tmp/altt-pytest-cache']
    report['pytest_argv']=arguments
    start=time.monotonic()
    exit_code=int(pytest.main(arguments,plugins=[RuntimeGuard()]))
    report['pytest_wall_sec']=time.monotonic()-start
    report['pytest_exit_code']=exit_code
    checkpoint('after_pytest_complete')
    report['source_hashes_after']=source_hashes()
    report['sources_unchanged']=report['source_hashes_before']==report['source_hashes_after']
    if not report['sources_unchanged']:
        raise RuntimeError('Tests modified baked Python sources')
except BaseException as exc:
    report['error']=f'{type(exc).__name__}: {exc}'
    report['traceback']=traceback.format_exc()
    exit_code=2
finally:
    report['exit_code']=exit_code
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'report':str(REPORT),'exit_code':exit_code,'outcomes':report.get('test_outcomes')}),flush=True)
raise SystemExit(exit_code)
