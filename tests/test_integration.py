"""Exercise the bundled CPython WASI runtime and real installed linters."""

import builtins
import io
import os
import shlex
import sys
import tempfile
import time
import urllib.request
from contextlib import ExitStack
from pathlib import Path
from typing import List

import pytest
from cantok import SimpleToken, TimeoutToken
from throng import throng
from throng.abstracts.results import RunResultProtocol
from throng.errors import CannotInstallDependencyError

from tests.test_installer import ENVIRONMENT, Index
from throng_wasm import (
    WasmIsolate,
    WasmManager,
    WasmResult,
    WasmRuntime,
    bootstrap,
    installer,
)
from throng_wasm.wasi import MemoryWasi


@pytest.fixture(scope='module')
def cpython() -> WasmRuntime:
    return WasmRuntime()


def test_entrypoint_in_process(tmp_path: Path) -> None:
    managers = throng(tmp_path)
    assert isinstance(managers['wasm'], WasmManager)
    assert 'wasmtime' not in managers


def test_install_queries_exact_guest_environment_without_project_imports(tmp_path: Path, cpython: WasmRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / 'json.py').write_text('raise AssertionError("Project module imported by interpreter probe")')
    observed = []

    def install(_root, packages, environment, check_alive, *, cancellation):
        check_alive()
        cancellation.check()
        observed.append((packages, environment))

    monkeypatch.setattr('throng_wasm.manager.install_packages', install)
    with WasmManager(tmp_path, runtime=cpython).scope as isolate:
        isolate.install('example')
    assert observed == [(('example',), ENVIRONMENT)]


@pytest.mark.parametrize('access', ['run', 'chain', 'scope', 'get'])
def test_exclusions_in_real_guest(tmp_path: Path, cpython: WasmRuntime, access: str) -> None:
    for name in ['private/secret', 'nested/drop.tmp', 'nested/keep.tmp', 'public']:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('data')
    manager = throng(tmp_path, exclude=['/private/', '*.tmp', '!nested/keep.tmp'])['wasm']
    assert isinstance(manager, WasmManager)
    manager.runtime = cpython
    code = '''from pathlib import Path
assert not Path('private').exists()
assert not Path('nested/drop.tmp').exists()
assert Path('nested/keep.tmp').read_text() == 'data'
assert Path('public').read_text() == 'data'
Path('generated.tmp').write_text('excluded from next snapshot')
Path('generated.py').write_text('retained in next snapshot')
print('ok')
'''
    command = 'python -c ' + shlex.quote(code)
    results: List[RunResultProtocol]
    if access == 'run':
        results = [manager.run(command)]
    elif access == 'chain':
        results = manager.chain(command, 'python -c "from pathlib import Path; assert Path(\'generated.tmp\').exists(); print(\'ok\')"')
    else:
        with ExitStack() as stack:
            isolate = manager.get(manager.read()) if access == 'get' else stack.enter_context(manager.scope)
            try:
                results = [isolate.run(command)]
                clone = manager.get(isolate.read())
                try:
                    assert not (clone.path / 'generated.tmp').exists()
                    assert (clone.path / 'generated.py').read_text() == 'retained in next snapshot'
                    assert clone.run('python -c "from pathlib import Path; assert not Path(\'generated.tmp\').exists()"').success
                finally:
                    clone.kill()
            finally:
                isolate.kill()
    assert all(result.success and result.stdout == 'ok\n' for result in results)
    assert (tmp_path / 'private/secret').read_text() == 'data'
    assert (tmp_path / 'nested/drop.tmp').read_text() == 'data'
    assert not (tmp_path / 'generated.py').exists()


@pytest.mark.parametrize('patterns', [[], ['*'], ['*.py', '*.json', 'packages/', 'scripts/', '.throng-wasm/']])
def test_installed_package_survives_exclusions(tmp_path: Path, cpython: WasmRuntime, monkeypatch: pytest.MonkeyPatch, patterns: List[str]) -> None:
    index = Index()
    index.add('example', files={'example-1.0.data/scripts/tool': b'#!python\nimport example; print(example.VERSION)\n'})
    monkeypatch.setattr(installer, 'download', index.download)
    manager = throng(tmp_path, exclude=patterns)['wasm']
    assert isinstance(manager, WasmManager)
    manager.runtime = cpython
    with manager.scope as isolate:
        isolate.install('example')
        state = isolate.read()

    def offline(*_args, **_kwargs):
        raise AssertionError('Restoring excluded projects must preserve installed packages offline.')

    monkeypatch.setattr(installer, 'download', offline)
    for clone in [manager.get(state), WasmIsolate(state, cpython)]:
        try:
            assert clone.run('python -c "import example; print(example.VERSION)"').stdout == '1.0\n'
            assert clone.run('python /scripts/tool').stdout == '1.0\n'
            clone.install('example')
        finally:
            clone.kill()


def test_python_cli(tmp_path: Path, cpython: WasmRuntime) -> None:
    (tmp_path / 'a b.py').write_text('import sys; print(sys.argv[1])')
    manager = WasmManager(tmp_path, runtime=cpython)
    with manager.scope as isolate:
        result = isolate.run('python "a b.py" "привет ; $HOME"')
        assert result.success
        assert result.stdout == 'привет ; $HOME\n'
        version = isolate.run('python --version').stdout
        assert version is not None
        assert version.startswith('Python 3.')
        result = isolate.run('python -c "import sys; print(123); print(456, file=sys.stderr); sys.exit(9)"')
        assert result.returncode == 9
        assert result.stdout == '123\n'
        assert result.stderr == '456\n'
        assert not isolate.run('python -m no_such_module_throng').success
        assert not isolate.run('python -c "this is invalid syntax"').success
        assert isolate.run('python -O -c "print(__debug__)"').stdout == 'False\n'


def test_guest_isolation(tmp_path: Path, cpython: WasmRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THRONG_PRIVATE_SECRET', 'host-only')
    code = '''import os, pathlib, sys
assert sys.platform == "wasi"
assert os.getcwd() == "/"
assert "THRONG_PRIVATE_SECRET" not in os.environ
assert not pathlib.Path("/etc/passwd").exists()
assert not pathlib.Path("/Users").exists()
try:
    pathlib.Path("/python/should-not-exist").write_text("no")
except OSError:
    pass
else:
    assert ALLOW_PRIVATE_WRITES
try:
    pathlib.Path("/packages/0/should-not-exist").write_text("no")
except OSError:
    pass
else:
    assert ALLOW_PRIVATE_WRITES
pathlib.Path("result").write_text("persisted")
print("isolated")
'''
    code = code.replace('ALLOW_PRIVATE_WRITES', str(False))
    manager = WasmManager(tmp_path, runtime=cpython)
    with manager.scope as isolate:
        result = isolate.run('python -c ' + shlex.quote(code))
        assert result.success, result.stderr
        assert result.stdout == 'isolated\n'
        result = isolate.run('python -c "print(open(\'result\').read())"')
        assert result.stdout == 'persisted\n'
        state = isolate.read()
    assert not (tmp_path / 'result').exists()
    if cpython.home is not None:
        assert not (cpython.home / 'should-not-exist').exists()
    restored = manager.get(state)
    try:
        assert restored.run('python -c "print(open(\'result\').read())"').stdout == 'persisted\n'
    finally:
        restored.kill()


@pytest.mark.parametrize(('tool', 'bad', 'message'), [
    ('mypy --no-incremental --cache-dir=/dev/null --no-site-packages --python-version=3.13', 'x: int = "bad"\n', 'Incompatible types'),
    ('pyflakes', 'import os\n', 'imported but unused'),
])
def test_linter_clean_and_error(tmp_path: Path, cpython: WasmRuntime, tool: str, bad: str, message: str) -> None:
    (tmp_path / 'clean.py').write_text('x: int = 1\n')
    (tmp_path / 'bad.py').write_text(bad)
    with WasmManager(tmp_path, runtime=cpython).scope as isolate:
        isolate.install('mypy==1.14.1' if tool.startswith('mypy') else 'pyflakes==3.3.2')
        clean = isolate.run(f'{tool} clean.py')
        assert clean.success, clean.stderr
        assert clean.returncode == 0
        # Removing the bad project file leaves a clean directory; dependencies
        # must not be discovered by recursive linters inspecting the project.
        assert isinstance(isolate, WasmIsolate)
        bad_source = isolate.path / 'bad.py'
        bad_source.unlink()
        assert isolate.run(f'{tool} .').success
        bad_source.write_text(bad)
        bad_result = isolate.run(f'{tool} bad.py')
        assert bad_result.returncode == 1, bad_result.stderr
        assert bad_result.stdout is not None
        assert message in bad_result.stdout


def test_real_python_cancellation(tmp_path: Path, cpython: WasmRuntime) -> None:
    with WasmManager(tmp_path, runtime=cpython).scope as isolate:
        assert isinstance(isolate, WasmIsolate)
        # Compile/initialize before starting a short execution deadline.
        assert isolate.run('python -c "pass"').success
        result = isolate.run('python -c "while True: pass"', TimeoutToken(0.1))
        assert result.killed_by_token
        assert result.returncode == 130
        assert isolate.run('python -c "print(1)"').stdout == '1\n'


def test_automatic_runtime_and_ls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THRONG_WASM_HOME', raising=False)
    monkeypatch.delenv('THRONG_WASM_PACKAGES', raising=False)
    monkeypatch.setenv('THRONG_WASM_CACHE', str(tmp_path / 'cache'))
    monkeypatch.setattr(bootstrap, '_bundle', None)

    def offline(*_args, **_kwargs):
        raise AssertionError('The packaged runtime must not access the network, even on first use.')

    monkeypatch.setattr(urllib.request, 'urlopen', offline)
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'bad.py').write_text('value: int = "bad"\n')
    (project / '.hidden').write_text('hidden')
    (project / 'argparse.py').write_text('raise RuntimeError("Project files must not override ls imports")\n')
    manager = throng(project)['wasm']
    assert not (tmp_path / 'cache').exists()
    result = manager.run('ls')
    assert result.success, result.stderr
    assert result.stdout == 'argparse.py\nbad.py\n'
    (project / 'argparse.py').unlink()

    with throng(project)['wasm'].scope as isolate:
        result = isolate.run('ls -la')
        assert result.success, result.stderr
        assert result.stdout is not None
        assert '.hidden' in result.stdout
        result = isolate.run('ls missing')
        assert result.returncode == 2
        assert result.stderr is not None
        assert 'ls: missing:' in result.stderr
        result = isolate.run('python -c "import sys; print(sys.platform)"')
        assert result.stdout == 'wasi\n'
        assert not isolate.run('mypy --version').success
        assert not isolate.run('pyflakes --version').success
        isolate.install('mypy==1.14.1', 'pyflakes==3.3.2')
        result = isolate.run('mypy --version')
        assert result.stdout == 'mypy 1.14.1 (compiled: no)\n'
        result = isolate.run('mypy --no-site-packages --no-incremental --cache-dir=/dev/null bad.py')
        assert result.returncode == 1, result.stderr
        assert result.stdout is not None
        assert 'Incompatible types' in result.stdout
        assert isolate.run('pyflakes bad.py').success


@pytest.mark.parametrize(('command', 'code', 'output', 'error'), [
    ('ls -a "a directory"', 0, '.\n..\n.hidden\na file\n', ''),
    ('ls -- "-a file"', 0, '-a file\n', ''),
    ('ls "missing directory"', 2, '', 'ls: missing directory:'),
])
def test_ls_preserves_quoted_paths_options_and_exit_status(tmp_path: Path, cpython: WasmRuntime, command: str,  # noqa: PLR0913
                                                         code: int, output: str, error: str) -> None:
    (tmp_path / 'a directory').mkdir()
    for name in ['a directory/.hidden', 'a directory/a file', '-a file']:
        (tmp_path / name).write_bytes(b'')
    result = WasmManager(tmp_path, runtime=cpython).run(command)
    assert result.returncode == code
    assert result.success == (code == 0)
    assert result.stdout == output
    if error:
        assert result.stderr is not None
        assert error in result.stderr
    else:
        assert result.stderr == ''


def test_installed_environment_is_private_portable_and_transactional(tmp_path: Path, cpython: WasmRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    index = Index()
    index.add('example', '1.0', files={'example/old.py': b''})
    index.add('example', '2.0', files={'example/new.py': b''})
    index.add('broken', tag='cp313-cp313-manylinux_2_17_x86_64')
    monkeypatch.setattr(installer, 'download', index.download)
    (tmp_path / 'project.py').write_text('value = 1\n')
    manager = WasmManager(tmp_path, runtime=cpython)
    with manager.scope as isolate, manager.scope as sibling:
        assert not isolate.run('python -c "import example"').success
        isolate.install('example==1.0')
        assert isolate.run('python -c "import example; print(example.VERSION)"').stdout == '1.0\n'
        assert not sibling.run('python -c "import example"').success
        isolate.run('python -c "open(\'created\', \'w\').write(\'preserved\')"')
        isolate.install('example==2.0')
        assert isolate.run('python -c "import example, example.new; print(example.VERSION)"').stdout == '2.0\n'
        assert not isolate.run('python -c "import example.old"').success
        with pytest.raises(CannotInstallDependencyError, match=r'broken.*No compatible'):
            isolate.install('broken')
        assert isolate.run('python -c "import example; print(example.VERSION)"').stdout == '2.0\n'
        assert isolate.run('python -c "print(open(\'created\').read())"').stdout == 'preserved\n'
        state = isolate.read()
        assert isinstance(isolate, WasmIsolate)
        path = isolate.path
    assert not path.exists()
    assert sorted(file.name for file in tmp_path.iterdir()) == ['project.py']

    def offline(_url: str) -> bytes:
        raise AssertionError('Restored dependencies must work offline.')

    monkeypatch.setattr(installer, 'download', offline)
    restored = manager.get(state)
    try:
        assert restored.run('python -c "import example; print(example.VERSION)"').stdout == '2.0\n'
        restored.install('example==2.0')
    finally:
        restored.kill()


@pytest.mark.parametrize('shebang', [b'#!python\n', b'#!python\r\n', b'#!pythonw\n'])
def test_wheel_scripts_inside_wasi_and_snapshot(tmp_path: Path, cpython: WasmRuntime, monkeypatch: pytest.MonkeyPatch, shebang: bytes) -> None:
    index = Index()
    source = (shebang + b'import example, sys\nassert sys.platform == "wasi"\n'
              b'print(example.VERSION, sys.argv[1])\nsys.exit(int(sys.argv[2]))\n')
    index.add('example', files={'example-1.0.data/scripts/example-tool': source})
    monkeypatch.setattr(installer, 'download', index.download)
    manager = WasmManager(tmp_path, runtime=cpython)
    command = 'python /scripts/example-tool "hello world"'
    with manager.scope as isolate, manager.scope as sibling:
        assert not isolate.run(f'{command} 0').success
        isolate.install('example')
        result = isolate.run(f'{command} 0')
        assert result.success, result.stderr
        assert result.stdout == '1.0 hello world\n'
        assert isolate.run(f'{command} 7').returncode == 7
        assert not sibling.run(f'{command} 0').success
        state = isolate.read()
        assert isinstance(isolate, WasmIsolate)
        original = isolate._root
    assert not original.exists()
    assert not list(tmp_path.iterdir())

    def offline(_url: str) -> bytes:
        raise AssertionError('Restored scripts must work offline.')

    monkeypatch.setattr(installer, 'download', offline)
    restored = WasmIsolate(state, WasmRuntime(cpython.home))
    try:
        result = restored.run(f'{command} 0')
        assert result.success, result.stderr
        assert result.stdout == '1.0 hello world\n'
        assert restored.run(f'{command} 7').returncode == 7
    finally:
        restored.kill()


def test_installed_linters_survive_snapshot_without_reinstall(tmp_path: Path, cpython: WasmRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / 'original'
    project.mkdir()
    (project / 'clean.py').write_text('value: int = 1\n')
    (project / 'bad.py').write_text('import os\nvalue: int = "wrong"\n')
    with WasmManager(project, runtime=cpython, exclude=['.throng-wasm/', 'packages/', 'scripts/', '*.json']).scope as isolate:
        assert not isolate.run('mypy --version').success
        assert not isolate.run('pyflakes --version').success
        isolate.install('mypy==1.14.1', 'pyflakes==3.3.2')
        state = isolate.read()
        assert isinstance(isolate, WasmIsolate)
        original_storage = isolate.path.parent
    assert not original_storage.exists()
    # Restoration must use only the snapshot, not even the original project.
    (project / 'clean.py').unlink()
    (project / 'bad.py').unlink()
    project.rmdir()

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Restoring installed dependencies must not download or install anything.')

    monkeypatch.setattr(installer, 'download', forbidden)
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr('throng_wasm.manager.install_packages', forbidden)
    empty_project = tmp_path / 'unrelated'
    empty_project.mkdir()
    # Use a new manager and runtime, with no external package directories.
    manager = WasmManager(empty_project, runtime=WasmRuntime(cpython.home))
    restored = manager.get(state)
    try:
        assert restored.run('mypy --version').stdout == 'mypy 1.14.1 (compiled: no)\n'
        version = restored.run('pyflakes --version')
        assert version.success
        assert version.stdout is not None
        assert version.stdout.startswith('3.3.2 ')
        imports = restored.run('python -c "import mypy, pyflakes, mypy_extensions, typing_extensions; print(\'ok\')"')
        assert imports.success, imports.stderr
        assert imports.stdout == 'ok\n'
        mypy = 'mypy --no-incremental --cache-dir=/dev/null --no-site-packages'
        for command, message in [(mypy, 'Incompatible types'), ('pyflakes', 'imported but unused')]:
            clean = restored.run(f'{command} clean.py')
            assert clean.success, clean.stderr
            bad = restored.run(f'{command} bad.py')
            assert bad.returncode == 1, bad.stderr
            assert bad.stdout is not None
            assert message in bad.stdout
    finally:
        restored.kill()
    assert not restored.path.parent.exists()


def test_pytest_inside_wasi(tmp_path: Path, cpython: WasmRuntime) -> None:
    (tmp_path / 'test_guest.py').write_text('''import logging
import os
import sys
import pytest

@pytest.fixture
def answer():
    return 42

@pytest.mark.parametrize("value", [0, 1, 2])
def test_parametrized(value):
    assert value + 1 > value

def test_fixture(answer):
    assert answer == 42

def test_wasi():
    assert sys.platform == "wasi"

def test_tmp_path(tmp_path):
    path = tmp_path / "hello.txt"
    path.write_text("hello")
    assert path.read_text() == "hello"

def test_capture(capsys):
    print("captured")
    assert capsys.readouterr().out == "captured\\n"

def test_logging(caplog):
    logging.warning("logged message")
    assert "logged message" in caplog.text

def test_monkeypatch(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TEST_VAR", "yes")
    assert os.environ["EXAMPLE_TEST_VAR"] == "yes"

def test_raises():
    with pytest.raises(ValueError):
        int("bad")

@pytest.mark.skip(reason="intentional skip")
def test_skip():
    pass

@pytest.mark.xfail(reason="intentional xfail", strict=True)
def test_xfail():
    assert False
''')
    (tmp_path / 'test_failure.py').write_text('def test_failure():\n    assert 1 == 2\n')
    command = ('python -m pytest -q --capture=sys -p no:faulthandler '
               '-p no:cacheprovider --log-file=/tmp/pytest.log --basetemp=/tmp/pytest')
    with WasmManager(tmp_path, runtime=cpython).scope as isolate:
        isolate.install('pytest==9.1.1')
        assert isolate.run('python -m pytest --version').stdout == 'pytest 9.1.1\n'
        passed = isolate.run(f'{command} test_guest.py', TimeoutToken(30))
        assert passed.success, (passed.stdout, passed.stderr)
        assert passed.stdout is not None
        assert '10 passed, 1 skipped, 1 xfailed' in passed.stdout
        assert passed.stderr == ''
        failed = isolate.run(f'{command} test_failure.py', TimeoutToken(30))
        assert failed.returncode == 1, (failed.stdout, failed.stderr)
        assert failed.stdout is not None
        assert '1 failed' in failed.stdout
        assert 'assert 1 == 2' in failed.stdout
        assert failed.stderr == ''


def test_no_host_writes_during_entire_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / 'source.txt').write_text('original')
    index = Index()
    index.add('example', files={'example-1.0.data/scripts/example': b'#!python\nimport example; print(example.VERSION)\n'})
    builtin_open, io_open, os_open = builtins.open, io.open, os.open

    def guarded_open(original, file, mode='r', *args, **kwargs):
        assert not any(flag in mode for flag in 'wax+'), (file, mode)
        return original(file, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND), (path, flags)
        return os_open(path, flags, *args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError('An isolate must not mutate the host filesystem.')

    with monkeypatch.context() as patch:
        patch.delenv('THRONG_WASM_HOME', raising=False)
        patch.delenv('THRONG_WASM_PACKAGES', raising=False)
        patch.setattr(sys, 'dont_write_bytecode', True)
        patch.setattr(bootstrap, '_bundle', None)
        patch.setattr(urllib.request, 'urlopen', forbidden)
        patch.setattr(installer, 'download', index.download)
        patch.setattr(builtins, 'open', lambda *a, **kw: guarded_open(builtin_open, *a, **kw))
        patch.setattr(io, 'open', lambda *a, **kw: guarded_open(io_open, *a, **kw))
        patch.setattr(os, 'open', guarded_os_open)
        for name in ['mkdir', 'makedirs', 'unlink', 'remove', 'rmdir', 'rename', 'replace', 'symlink', 'link', 'chmod', 'utime']:
            patch.setattr(os, name, forbidden)
        patch.setattr(tempfile, 'TemporaryDirectory', forbidden)
        manager = WasmManager(tmp_path)
        with manager.scope as isolate:
            isolate.install('example')
            assert isolate.run('python /scripts/example').stdout == '1.0\n'
            code = ('from pathlib import Path; import tempfile; '
                    'Path(".cache").mkdir(); Path(".cache/result").write_text("saved"); '
                    'Path(tempfile.gettempdir(), "scratch").write_text("temporary"); '
                    'print(Path("source.txt").read_text())')
            assert isolate.run('python -c ' + shlex.quote(code)).stdout == 'original\n'
            state = isolate.read()
        patch.setattr(installer, 'download', forbidden)
        restored = manager.get(state)
        try:
            assert restored.run('python /scripts/example').stdout == '1.0\n'
            assert restored.run('python -c "print(open(\'.cache/result\').read())"').stdout == 'saved\n'
            assert restored.run('python -c "from pathlib import Path; print(Path(\'/tmp/scratch\').exists())"').stdout == 'False\n'
        finally:
            restored.kill()
    assert sorted(p.name for p in tmp_path.iterdir()) == ['source.txt']


def test_guest_filesystem_operations(tmp_path: Path, cpython: WasmRuntime) -> None:
    code = '''import io, os, pathlib, time
p = pathlib.Path('nested')
p.mkdir()
with (p / 'file').open('w+b') as f:
    f.write(b'hello')
    f.seek(1)
    assert f.read(2) == b'el'
    f.seek(0, 2)
    assert f.tell() == 5
    f.truncate(2)
assert (p / 'file').read_bytes() == b'he'
with (p / 'file').open('ab') as f:
    f.write(b'y')
assert (p / 'file').read_bytes() == b'hey'
with (p / 'file').open('rb') as f:
    (p / 'file').unlink()
    assert f.read() == b'hey'
(p / 'file').write_text('new')
os.utime(p / 'file', ns=(1000000000, 2000000000))
assert (p / 'file').stat().st_mtime_ns == 2000000000
p.rename('renamed')
assert os.listdir('renamed') == ['file']
pathlib.Path('renamed/file').unlink()
pathlib.Path('renamed').rmdir()
with open('/dev/null', 'wb') as f:
    assert f.write(b'ignored') == 7
with open('/dev/null', 'rb') as f:
    assert f.read() == b''
with open('/dev/null', 'r+b') as f:
    assert f.seek(100) == 0
assert len(os.urandom(16)) == 16
time.sleep(0.001)
print('filesystem ok')
'''
    with WasmManager(tmp_path, runtime=cpython).scope as isolate:
        result = isolate.run('python -c ' + shlex.quote(code))
        assert result.success, result.stderr
        assert result.stdout == 'filesystem ok\n'
        started = time.monotonic()
        cancelled = isolate.run('python -c "import time; time.sleep(1000)"', TimeoutToken(0.1))
        assert isinstance(cancelled, WasmResult)
        assert cancelled.killed_by_token
        assert time.monotonic() - started < 5


@pytest.mark.parametrize('owner', ['manager', 'isolate'])
def test_chain_shares_files_but_not_process_state(tmp_path: Path, cpython: WasmRuntime, owner: str) -> None:
    manager = WasmManager(tmp_path, runtime=cpython)
    isolate = manager.get(manager.read())
    subject = manager if owner == 'manager' else isolate
    first = ('import os, sys; from pathlib import Path; '
             'Path("saved").write_text("kept"); Path("/tmp/ephemeral").write_text("tmp"); '
             'Path("directory").mkdir(); os.chdir("directory"); '
             'os.environ["CUSTOM"]="set"; sys.custom=1; sys.exit(5)')
    second = '''import os, sys
from pathlib import Path
assert os.getcwd() == '/'
assert 'CUSTOM' not in os.environ
assert not hasattr(sys, 'custom')
assert not Path('/tmp/ephemeral').exists()
print(Path('saved').read_text())
'''
    try:
        assert subject.chain() == []
        results = subject.chain('python -c ' + shlex.quote(first), 'python -c ' + shlex.quote(second))
        assert [result.returncode for result in results] == [5, 0]
        assert results[1].stdout == 'kept\n'
        assert results[1].stderr == ''
        assert not list(tmp_path.iterdir())
    finally:
        isolate.kill()


def test_cancellation_preserves_output_files_and_skips_remaining_commands(tmp_path: Path, cpython: WasmRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    token = SimpleToken()
    write = MemoryWasi.w_fd_write

    def cancel_after_ready(wasi, memory, fd, *args):
        write(wasi, memory, fd, *args)
        if fd == 1 and wasi.stdout.data == b'ready\n':
            token.cancel()

    monkeypatch.setattr(MemoryWasi, 'w_fd_write', cancel_after_ready)
    code = '''import sys
open('saved', 'w').write('kept')
print('diagnostic', file=sys.stderr)
print('ready')
while True: pass
'''
    with WasmManager(tmp_path, runtime=cpython).scope as runner:
        assert isinstance(runner, WasmIsolate)
        assert runner.run('python -c pass').success
        results = runner.chain('python -c ' + shlex.quote(code), 'unsupported command',
                               'python -c "open(\'must-not-exist\', \'w\').close()"', token=token + TimeoutToken(5))
        assert [result.returncode for result in results] == [130, None, None]
        assert all(isinstance(result, WasmResult) and result.killed_by_token for result in results)
        assert results[0].stdout == 'ready\n'
        assert results[0].stderr is not None
        assert results[0].stderr.startswith('diagnostic\n')
        assert not (runner.path / 'must-not-exist').exists()
        clone = WasmIsolate(runner.read(), cpython)
        try:
            assert clone.run('python -c "print(open(\'saved\').read())"').stdout == 'kept\n'
        finally:
            clone.kill()


def test_external_library_order_readonly_and_installed_precedence(tmp_path: Path, cpython: WasmRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second, project = (tmp_path / name for name in ['first', 'second', 'project'])
    for directory, value in [(first, 'first'), (second, 'second')]:
        directory.mkdir()
        (directory / 'shadow.py').write_text(f'VERSION = "{value}"\n')
        (directory / f'{value}.py').write_text(f'NAME = "{value}"\n')
    project.mkdir()
    runtime = WasmRuntime(cpython.home, packages=[first, second])
    index = Index()
    index.add('shadow')
    monkeypatch.setattr(installer, 'download', index.download)
    with WasmManager(project, runtime=runtime).scope as isolate:
        result = isolate.run('python -P -c "import shadow, first, second; print(shadow.VERSION, first.NAME, second.NAME)"')
        assert result.stdout == 'first first second\n', result.stderr
        isolate.install('shadow')
        result = isolate.run('python -P -c "import shadow; print(shadow.VERSION)"')
        assert result.stdout == '1.0\n', result.stderr
        code = '''import first
from pathlib import Path
try:
    Path(first.__file__).write_text('must not write')
except OSError:
    print('readonly')
else:
    raise AssertionError('External packages must be readonly')
'''
        result = isolate.run('python -P -c ' + shlex.quote(code))
        assert result.success, result.stderr
        assert result.stdout == 'readonly\n'
    assert (first / 'first.py').read_text() == 'NAME = "first"\n'
    assert not list(project.iterdir())
