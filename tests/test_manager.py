import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from zipfile import BadZipFile

import pytest
from cantok import SimpleToken

from throng_wasmtime import WasmIsolate, WasmManager, WasmRuntime
from throng_wasmtime.state import restore, snapshot


def test_snapshots_and_lifecycle(tmp_path: Path, runtime: WasmRuntime) -> None:
    source = tmp_path / 'project'
    source.mkdir()
    (source / 'file').write_text('original')
    manager = WasmManager(source, runtime=runtime)
    state = manager.read()
    (source / 'file').write_text('later')
    isolate = manager.get(state)
    assert (isolate.path / 'file').read_text() == 'original'
    (isolate.path / 'file').write_text('guest')
    second = manager.get(isolate.read())
    assert (second.path / 'file').read_text() == 'guest'
    assert (source / 'file').read_text() == 'later'
    assert isolate.run('python --version').success
    isolate.kill()
    isolate.kill()
    assert not isolate.path.exists()
    with pytest.raises(RuntimeError, match='destroyed'):
        isolate.run('python')
    with pytest.raises(RuntimeError, match='destroyed'):
        isolate.read()
    second.kill()


def test_manager_scope_and_chain(tmp_path: Path, runtime: WasmRuntime) -> None:
    manager = WasmManager(tmp_path, runtime=runtime)
    assert manager.run('python').success
    assert all(result.success for result in manager.chain('python', 'python3'))
    with pytest.raises(LookupError), manager.scope as isolate:
        raise LookupError
    assert isinstance(isolate, WasmIsolate)
    assert not isolate.path.exists()
    token = SimpleToken()
    token.cancel()
    with manager.scope as isolate:
        assert isinstance(isolate, WasmIsolate)
        result = isolate.run('invalid command', token)
        assert result.killed_by_token
        assert result.returncode is None
        assert all(not result.success for result in isolate.chain('python', 'python', token=token))


def test_environment_lazy(tmp_path: Path, wasm_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (wasm_home / 'python.wasm').write_text('(module (func (export "_start")))')
    monkeypatch.setenv('THRONG_WASM_HOME', str(wasm_home))
    monkeypatch.delenv('THRONG_WASM_PACKAGES', raising=False)
    manager = WasmManager(tmp_path)
    isolate = manager.get(manager.read())
    assert isolate.runtime is manager.runtime
    isolate.kill()
    isolated = WasmIsolate(snapshot(tmp_path))
    assert isolated.run('python').success
    isolated.kill()


def test_failed_restore_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    killed = []
    kill = WasmIsolate.kill

    def record_cleanup(self):
        if not self._root.root.closed:
            killed.append(self.path)
        kill(self)

    monkeypatch.setattr(WasmIsolate, 'kill', record_cleanup)
    with pytest.raises(BadZipFile):
        WasmIsolate(b'not a snapshot')
    assert len(killed) == 1
    assert not killed[0].exists()


def test_plugin_discovery(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.pop('THRONG_WASM_HOME', None)
    result = subprocess.run(
        [sys.executable, '-c', 'from throng import throng; from throng_wasmtime import WasmManager; managers = throng(); assert isinstance(managers["wasm"], WasmManager); assert "wasmtime" not in managers'],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_kill_running_isolate(tmp_path: Path, wasm_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (wasm_home / 'python.wasm').write_text('''(module
      (import "wasi_snapshot_preview1" "sched_yield" (func $ready (result i32)))
      (memory (export "memory") 1)
      (func (export "_start") (drop (call $ready)) (loop $spin br $spin)))''')
    runtime = WasmRuntime(wasm_home)
    runtime._load()
    isolate = WasmIsolate(snapshot(tmp_path), runtime)
    started = Event()

    def signal_start(*_args):
        started.set()

    monkeypatch.setattr('throng_wasmtime.wasi.MemoryWasi.w_sched_yield', signal_start)
    with ThreadPoolExecutor() as executor:
        future = executor.submit(isolate.run, 'python')
        assert started.wait(5)
        isolate.kill()
        assert future.result(timeout=5).returncode == 130
    assert not isolate.path.exists()


def test_clones_do_not_share_mutable_files_or_lifetime(tmp_path: Path, runtime: WasmRuntime) -> None:
    (tmp_path / 'source').write_text('original')
    manager = WasmManager(tmp_path, runtime=runtime)
    state = manager.read()
    first, second = manager.get(state), manager.get(state)
    try:
        (first.path / 'source').write_text('changed')
        (first.path / 'new').write_text('private')
        first.kill()
        assert (second.path / 'source').read_text() == 'original'
        assert not (second.path / 'new').exists()
        assert second.run('python').success
        assert (tmp_path / 'source').read_text() == 'original'
    finally:
        first.kill()
        second.kill()


@pytest.mark.parametrize('method', ['run', 'chain'])
def test_manager_cleans_up_after_command_error(tmp_path: Path, runtime: WasmRuntime, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    manager = WasmManager(tmp_path, runtime=runtime)
    created = []
    get = manager._get

    def record(*args, **kwargs):
        isolate = get(*args, **kwargs)
        created.append(isolate)
        return isolate

    monkeypatch.setattr(manager, '_get', record)
    with pytest.raises(ValueError, match='Unsupported command'):
        getattr(manager, method)('unsupported')
    assert len(created) == 1
    assert not created[0].path.exists()
    assert manager.run('python').success
    assert not created[1].path.exists()
    assert manager.chain() == []


def test_manager_snapshot_filters_are_recursive_and_configurable(tmp_path: Path, runtime: WasmRuntime) -> None:
    for name in ['.git/config', 'nested/.git/config', 'venv/package', 'nested/keep.py', 'private/data']:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('data')
    default = restore(WasmManager(tmp_path, runtime=runtime).read())
    assert not (default / '.git').exists()
    assert not (default / 'nested/.git').exists()
    assert not (default / 'venv').exists()
    assert (default / 'nested/keep.py').is_file()
    custom = restore(WasmManager(tmp_path, runtime=runtime, exclude=('private',)).read())
    assert (custom / '.git/config').is_file()
    assert (custom / 'venv/package').is_file()
    assert not (custom / 'private').exists()


def test_kill_signals_before_mutex_and_concurrent_cleanup_is_idempotent(tmp_path: Path, runtime: WasmRuntime) -> None:
    isolate = WasmManager(tmp_path, runtime=runtime).get(snapshot(tmp_path))
    (isolate.path / 'data').write_bytes(b'project')
    isolate._environment.mkdir()
    (isolate._environment / 'data').write_bytes(b'environment')
    root = isolate._root.root
    with ThreadPoolExecutor(max_workers=2) as executor:
        with isolate._lock:
            pending = [executor.submit(isolate.kill) for _ in range(2)]
            assert isolate._stopped.wait(3)
            assert all(not future.done() for future in pending)
            assert not root.closed
        for future in pending:
            future.result(timeout=3)
    isolate.kill()
    assert root.closed
    assert root.children == {}
    for operation in [lambda: isolate.run('python'), isolate.read, lambda: isolate.install('tool')]:
        with pytest.raises(RuntimeError, match='destroyed'):
            operation()
