import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread

import pytest
import wasmtime
from cantok import SimpleToken

from throng_wasm import WasmRuntime, runtime as runtime_module
from throng_wasm.memory import MemoryPath
from throng_wasm.runtime import command_arguments
from throng_wasm.state import restore, snapshot


@pytest.mark.parametrize(('command', 'expected'), [
    ('python -c "print(1)"', ['-c', 'print(1)']),
    ('python3 "a b.py" ";"', ['a b.py', ';']),
    ('python --version', ['--version']),
    ('mypy a.py', ['-m', 'mypy', 'a.py']),
    ('pyflakes a.py', ['-m', 'pyflakes', 'a.py']),
    ('flake8 a.py', ['-m', 'flake8', 'a.py']),
    ('pycodestyle a.py', ['-m', 'pycodestyle', 'a.py']),
])
def test_commands(command: str, expected: list) -> None:
    assert command_arguments(command) == expected


@pytest.mark.parametrize('command', ['', '   ', 'sh -c ls', 'ruff check .', 'python "'])
def test_bad_commands(command: str) -> None:
    with pytest.raises(ValueError, match=r"empty|Unsupported|Ruff|quotation"):
        command_arguments(command)


def test_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv('THRONG_WASM_HOME', raising=False)
    monkeypatch.setenv('THRONG_WASM_CACHE', str(tmp_path / 'cache'))
    monkeypatch.delenv('THRONG_WASM_PACKAGES', raising=False)
    runtime = WasmRuntime.from_environment()
    assert runtime.home is None
    assert runtime.packages == ()
    assert not (tmp_path / 'cache').exists()
    monkeypatch.setenv('THRONG_WASM_PACKAGES', '')
    assert WasmRuntime.from_environment().packages == ()
    monkeypatch.setenv('THRONG_WASM_PACKAGES', str(tmp_path))
    assert WasmRuntime.from_environment().packages == (tmp_path,)
    monkeypatch.setenv('THRONG_WASM_HOME', str(tmp_path))
    monkeypatch.delenv('THRONG_WASM_PACKAGES', raising=False)
    assert WasmRuntime.from_environment().packages == ()
    monkeypatch.setenv('THRONG_WASM_PACKAGES', str(tmp_path))
    assert WasmRuntime.from_environment().packages == (tmp_path,)


def test_invalid_config(wasm_home: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='positive'):
        WasmRuntime(wasm_home, memory_limit=0)
    runtime = WasmRuntime(wasm_home / 'missing')
    with pytest.raises(FileNotFoundError, match='library directory'):
        runtime.run([], MemoryPath(), SimpleToken(), Event())
    runtime = WasmRuntime(wasm_home, packages=[tmp_path / 'missing'])
    with pytest.raises(NotADirectoryError):
        runtime.run([], MemoryPath(), SimpleToken(), Event())
    runtime = WasmRuntime(wasm_home)
    with pytest.raises(FileNotFoundError):
        runtime.run([], MemoryPath(), SimpleToken(), Event())
    (wasm_home / 'python.wasm').write_bytes(b'not wasm')
    with pytest.raises(wasmtime.WasmtimeError):
        runtime.run([], MemoryPath(), SimpleToken(), Event())


def test_fresh_memory_and_outputs(wasm_home: Path, tmp_path: Path) -> None:
    (wasm_home / 'python.wasm').write_text(r'''
    (module
      (import "wasi_snapshot_preview1" "fd_write" (func $write (param i32 i32 i32 i32) (result i32)))
      (memory (export "memory") 1)
      (data (i32.const 16) "hello\ff")
      (func (export "_start")
        (i32.store (i32.const 0) (i32.const 16))
        (i32.store (i32.const 4) (i32.const 6))
        (drop (call $write (i32.const 1) (i32.const 0) (i32.const 1) (i32.const 8)))
        (drop (call $write (i32.const 2) (i32.const 0) (i32.const 1) (i32.const 8)))
        (i32.store (i32.const 16) (i32.const 0))))
    ''')
    runtime = WasmRuntime(wasm_home, packages=[tmp_path])
    first = runtime.run([], MemoryPath(), SimpleToken(), Event(), packages=[restore(snapshot(tmp_path))], scripts=MemoryPath())
    second = runtime.run([], MemoryPath(), SimpleToken(), Event(), packages=[restore(snapshot(tmp_path))], scripts=MemoryPath())
    assert first == second
    assert first.success
    assert first.returncode == 0
    assert first.stdout == 'hello\ufffd'
    assert first.stderr == 'hello\ufffd'


@pytest.mark.parametrize(('body', 'code'), [('unreachable', 1), ('', 0)])
def test_trap_and_success(wasm_home: Path, body: str, code: int) -> None:
    (wasm_home / 'python.wasm').write_text(f'(module (func (export "_start") {body}))')
    result = WasmRuntime(wasm_home).run([], MemoryPath(), SimpleToken(), Event())
    assert result.returncode == code
    assert result.success == (code == 0)
    assert bool(result.stderr) == bool(code)


def test_exit(wasm_home: Path) -> None:
    (wasm_home / 'python.wasm').write_text('''(module
      (import "wasi_snapshot_preview1" "proc_exit" (func $exit (param i32)))
      (memory (export "memory") 1)
      (func (export "_start") (call $exit (i32.const 7))))''')
    result = WasmRuntime(wasm_home).run([], MemoryPath(), SimpleToken(), Event())
    assert result.returncode == 7
    assert not result.success


def test_bad_export(wasm_home: Path) -> None:
    (wasm_home / 'python.wasm').write_text('(module (memory (export "_start") 1))')
    with pytest.raises(TypeError, match='_start'):
        WasmRuntime(wasm_home).run([], MemoryPath(), SimpleToken(), Event())


def test_memory_limit(wasm_home: Path) -> None:
    (wasm_home / 'python.wasm').write_text('''(module
      (memory 1)
      (func (export "_start")
        (if (i32.ne (memory.grow (i32.const 1)) (i32.const -1)) (then unreachable))))''')
    result = WasmRuntime(wasm_home, memory_limit=65536, cache=False).run([], MemoryPath(), SimpleToken(), Event())
    assert result.success


def test_cancel_before_start(runtime: WasmRuntime) -> None:
    token = SimpleToken()
    token.cancel()
    result = runtime.run([], MemoryPath(), token, Event())
    assert result.returncode is None
    assert result.stdout is None
    assert result.killed_by_token
    stopped = Event()
    stopped.set()
    result = runtime.run([], MemoryPath(), SimpleToken(), stopped)
    assert not result.killed_by_token
    assert result.returncode is None


def test_cancel_loop_and_reuse(wasm_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (wasm_home / 'python.wasm').write_text('''(module
      (import "wasi_snapshot_preview1" "sched_yield" (func $ready (result i32)))
      (memory (export "memory") 1)
      (func (export "_start") (drop (call $ready)) (loop $spin br $spin)))''')
    runtime = WasmRuntime(wasm_home)
    runtime._load()
    assert runtime._engine is not None
    started = Event()
    monkeypatch.setattr('throng_wasm.wasi.MemoryWasi.w_sched_yield', lambda *_a: started.set())
    # A second invocation must get a fresh epoch deadline after the first trap.
    with ThreadPoolExecutor(max_workers=1) as executor:
        for _ in range(2):
            started.clear()
            token = SimpleToken()
            pending = executor.submit(runtime.run, [], MemoryPath(), token, Event())
            try:
                assert started.wait(5)
                token.cancel()
                result = pending.result(timeout=5)
                assert result.returncode == 130
                assert result.killed_by_token
                assert not result.success
            finally:
                token.cancel()
                if not pending.done():
                    runtime._engine.increment_epoch()


def test_stop_running(wasm_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (wasm_home / 'python.wasm').write_text('''(module
      (import "wasi_snapshot_preview1" "sched_yield" (func $ready (result i32)))
      (memory (export "memory") 1)
      (func (export "_start") (drop (call $ready)) (loop $spin br $spin)))''')
    runtime = WasmRuntime(wasm_home)
    runtime._load()
    stopped, started = Event(), Event()

    def signal_start(*_args):
        started.set()

    monkeypatch.setattr('throng_wasm.wasi.MemoryWasi.w_sched_yield', signal_start)
    with ThreadPoolExecutor() as executor:
        future = executor.submit(runtime.run, [], MemoryPath(), SimpleToken(), stopped)
        assert started.wait(5)
        stopped.set()
        result = future.result(timeout=5)
    assert result.returncode == 130
    assert not result.killed_by_token


def test_cancelling_one_command_does_not_cancel_queued_command(wasm_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (wasm_home / 'python.wasm').write_text('''(module
      (import "wasi_snapshot_preview1" "args_sizes_get" (func $args (param i32 i32) (result i32)))
      (memory (export "memory") 1)
      (func (export "_start")
        (drop (call $args (i32.const 0) (i32.const 4)))
        (if (i32.gt_u (i32.load (i32.const 0)) (i32.const 1))
            (then (loop $spin br $spin)))))''')
    runtime = WasmRuntime(wasm_home)
    started = Event()
    execute = runtime._execute

    def signal_start(*args, **kwargs):
        started.set()
        return execute(*args, **kwargs)

    monkeypatch.setattr(runtime, '_execute', signal_start)
    token = SimpleToken()
    with ThreadPoolExecutor() as executor:
        first = executor.submit(runtime.run, ['spin'], MemoryPath(), token, Event())
        assert started.wait(5)
        second = executor.submit(runtime.run, [], MemoryPath(), SimpleToken(), Event())
        assert not second.done()
        token.cancel()
        assert first.result(timeout=5).killed_by_token
        assert second.result(timeout=5).success



def test_disk_cache_is_rejected(wasm_home: Path) -> None:
    with pytest.raises(ValueError, match='Disk compilation caches are disabled'):
        WasmRuntime(wasm_home, cache=True)


def test_parallel_runtimes_exit_independently(wasm_home: Path) -> None:
    (wasm_home / 'python.wasm').write_text('''(module
      (import "wasi_snapshot_preview1" "proc_exit" (func $exit (param i32)))
      (memory (export "memory") 1)
      (func (export "_start") (call $exit (i32.const 9))))''')
    runtimes = [WasmRuntime(wasm_home), WasmRuntime(wasm_home)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda runtime: runtime.run([], MemoryPath(), SimpleToken(), Event()), runtimes))
    assert [result.returncode for result in results] == [9, 9]


def test_cli_does_not_expand_shell_syntax() -> None:
    assert command_arguments('python script.py "" "$HOME" "$(echo boom)" "*" ";" "|" ">"') == [
        'script.py', '', '$HOME', '$(echo boom)', '*', ';', '|', '>',
    ]


def test_environment_preserves_package_order_and_skips_empty_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = tmp_path / 'one', tmp_path / 'two'
    monkeypatch.setenv('THRONG_WASM_PACKAGES', os.pathsep.join(['', str(first), '', str(second), '']))
    assert WasmRuntime.from_environment().packages == (first, second)


@pytest.mark.parametrize('source', ['(module)', '(module (memory 2) (func (export "_start")))'])
def test_instantiation_failure_releases_runtime_mutex(wasm_home: Path, source: str) -> None:
    (wasm_home / 'python.wasm').write_text(source)
    runtime = WasmRuntime(wasm_home, memory_limit=65536)
    for _ in range(2):
        with pytest.raises((KeyError, wasmtime.WasmtimeError)):
            runtime.run([], MemoryPath(), SimpleToken(), Event())
        assert runtime.lock.acquire(blocking=False)
        runtime.lock.release()


@pytest.mark.parametrize(('source', 'exception'), [
    ('(module (func (export "_start")))', None),
    ('(module (func (export "_start") unreachable))', None),
    ('(module (import "wasi_snapshot_preview1" "proc_exit" (func $exit (param i32))) (memory (export "memory") 1) (func (export "_start") (call $exit (i32.const 7))))', None),
    ('(module)', KeyError),
    ('(module (memory (export "_start") 1))', TypeError),
    ('(module (memory 2) (func (export "_start")))', wasmtime.WasmtimeError),
])
def test_watchdog_is_joined_before_store_closes_and_runtime_can_retry(wasm_home: Path, monkeypatch: pytest.MonkeyPatch, source: str, exception) -> None:
    (wasm_home / 'python.wasm').write_text(source)
    runtime = WasmRuntime(wasm_home, memory_limit=65536)
    watchers, closed = [], []
    delete = wasmtime.Store._delete

    def thread(*args, **kwargs):
        watcher = Thread(*args, **kwargs)
        watchers.append(watcher)
        return watcher

    def delete_store(store, pointer):
        assert all(not watcher.is_alive() for watcher in watchers)
        closed.append(True)
        delete(store, pointer)

    monkeypatch.setattr(runtime_module, 'Thread', thread)
    monkeypatch.setattr(wasmtime.Store, '_delete', delete_store)
    for _ in range(2):
        if exception is None:
            runtime.run([], MemoryPath(), SimpleToken(), Event())
        else:
            with pytest.raises(exception):
                runtime.run([], MemoryPath(), SimpleToken(), Event())
        assert not watchers[-1].is_alive()
        assert runtime.lock.acquire(blocking=False)
        runtime.lock.release()
    assert len(watchers) == 2
    assert len(closed) == 2
