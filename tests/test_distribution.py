"""Build and validate release artifacts as part of the regular test suite."""

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
from configparser import ConfigParser
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

from throng_wasmtime.bootstrap import BUNDLE_RESOURCE, BUNDLE_SHA256


@pytest.fixture(scope='session')
def distribution_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = Path(__file__).resolve().parents[1]
    workspace = tmp_path_factory.mktemp('distribution')
    source = workspace / 'source'
    source.mkdir()
    for name in ['pyproject.toml', 'README.md', 'LICENSE']:
        shutil.copy2(project / name, source / name)
    shutil.copytree(project / 'throng_wasmtime', source / 'throng_wasmtime',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # Each pytest worker builds in its own copy, without modifying the checkout.
    directory = workspace / 'dist'
    env = {key: value for key, value in os.environ.items() if key not in {'PYTHONPATH', 'COVERAGE_PROCESS_START'}}
    result = subprocess.run([sys.executable, '-m', 'build', '--outdir', str(directory)], cwd=source, env=env,
                            capture_output=True, text=True, check=False, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    return directory


def test_archives_contain_runtime_and_notices(distribution_directory: Path) -> None:
    wheel, = distribution_directory.glob('*.whl')
    sdist, = distribution_directory.glob('*.tar.gz')
    with ZipFile(wheel) as archive:
        data = archive.read(f'throng_wasmtime/{BUNDLE_RESOURCE}')
        notices = archive.read('throng_wasmtime/data/RUNTIME_LICENSES.txt')
        assert 'throng_wasmtime/py.typed' in archive.namelist()
        metadata_name, = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
        assert SpecifierSet(metadata['Requires-Python']) == SpecifierSet('>=3.8')
        requirements = {str(Requirement(value)) for value in metadata.get_all('Requires-Dist', [])}
        assert {'throng>=0.0.4', 'dirstree>=0.0.12', 'pathspec>=0.12.0', 'microbenchmark>=0.0.3'} <= requirements
        entry_points, = [name for name in archive.namelist() if name.endswith('.dist-info/entry_points.txt')]
        registration = ConfigParser()
        registration.read_string(archive.read(entry_points).decode())
        assert dict(registration.items('throng')) == {'wasm': 'throng_wasmtime.plugin:wasm'}
        assert 'throng_wasmtime/python-3.13.11-wasi_sdk-24.zip' not in archive.namelist()
        assert 'throng_wasmtime/RUNTIME_LICENSES.txt' not in archive.namelist()
    assert hashlib.sha256(data).hexdigest() == BUNDLE_SHA256
    assert b'PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2' in notices
    with tarfile.open(sdist) as archive:
        runtime, = [member for member in archive.getmembers() if member.name.endswith('/' + BUNDLE_RESOURCE)]
        resource = archive.extractfile(runtime)
        assert resource is not None
        assert resource.read() == data
        license_member, = [member for member in archive.getmembers() if member.name.endswith('/data/RUNTIME_LICENSES.txt')]
        resource = archive.extractfile(license_member)
        assert resource is not None
        assert resource.read() == notices


def test_wheel_starts_offline_in_fresh_process(distribution_directory: Path, tmp_path: Path) -> None:
    wheel, = distribution_directory.glob('*.whl')
    code = '''import socket, sys, urllib.request
sys.path.insert(0, sys.argv[1])
def forbidden(*args, **kwargs):
    raise AssertionError('Runtime startup must not access the network')
socket.socket.connect = forbidden
socket.create_connection = forbidden
urllib.request.urlopen = forbidden
import throng_wasmtime
assert throng_wasmtime.__file__.startswith(sys.argv[1])
from throng import throng
from throng_wasmtime import WasmRuntime, WasmIsolate
from throng_wasmtime.benchmarks import checked_wasm
managers = throng(exclude=['*.tmp'])
assert 'wasmtime' not in managers
manager = managers['wasm']
with manager.scope as isolate:
    checked_wasm(isolate, 'python -c pass')
    result = isolate.run('python -c "import sys; print(sys.platform)"')
    assert result.success, result.stderr
    assert result.stdout == 'wasi\\n', result.stdout
    assert isolate.run('ls').stdout == ''
    assert isolate.run('python --version').stdout == 'Python 3.13.11\\n'
    assert isolate.run('python -c "open(\\'created\\', \\'w\\').write(\\'saved\\')"').success
    assert isolate.run('python -c "open(\\'excluded.tmp\\', \\'w\\').write(\\'private\\')"').success
    state = isolate.read()
restored = WasmIsolate(state, runtime=WasmRuntime())
try:
    assert not (restored.path / 'excluded.tmp').exists()
    result = restored.run('python -c "print(open(\\'created\\').read())"')
    assert result.success, result.stderr
    assert result.stdout == 'saved\\n', result.stdout
finally:
    restored.kill()
print('offline wheel ok')
'''
    env = {key: value for key, value in os.environ.items()
           if key not in {'PYTHONPATH', 'COVERAGE_PROCESS_START'} and not key.startswith('THRONG_WASM_')}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run([sys.executable, '-c', code, str(wheel)], cwd=tmp_path, env=env, capture_output=True, text=True, check=False, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == 'offline wheel ok\n'
    assert not list(tmp_path.iterdir())
