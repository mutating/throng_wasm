"""Cancellation must cover preparation, queues, execution and transactions."""

import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from io import BytesIO
from pathlib import Path
from threading import Event
from typing import Callable, ContextManager, Dict, Optional, Type
from zipfile import ZipExtFile

import pytest
from cantok import (
    AbstractToken,
    CancellationError,
    ConditionToken,
    CounterToken,
    SimpleToken,
    TimeoutCancellationError,
    TimeoutToken,
)

from throng_wasmtime import WasmIsolate, WasmManager, WasmResult, WasmRuntime
from throng_wasmtime.memory import MemoryPath
from throng_wasmtime.state import restore, snapshot
from throng_wasmtime.wasi import MemoryWasi


@pytest.mark.parametrize('owner', ['isolate', 'runtime', 'manager'])
def test_cancel_while_waiting_for_mutex(tmp_path: Path, runtime: WasmRuntime, owner: str) -> None:
    manager = WasmManager(tmp_path, runtime=runtime)
    isolate = manager.get(manager.read())
    locks: Dict[str, ContextManager[object]] = {'isolate': isolate._lock, 'runtime': runtime.lock, 'manager': manager._lock}
    lock = locks[owner]
    entered = Event()
    def signal_check():
        entered.set()
        return False

    token = SimpleToken(ConditionToken(signal_check))
    with ThreadPoolExecutor() as executor:
        with lock:
            pending = executor.submit(manager.run if owner == 'manager' else isolate.run, 'python', token)
            assert entered.wait(3)
            token.cancel()
            try:
                result = pending.result(timeout=1)
            except FutureTimeout:
                pytest.fail('Cancellation waited for the occupied mutex')
        assert result.killed_by_token
        assert result.returncode is None
    isolate.kill()


def test_cancel_preparation_without_starting_guest(runtime: WasmRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release, finished = Event(), Event(), Event()
    load = runtime._load
    calls = []

    def slow_load():
        calls.append('load')
        entered.set()
        try:
            assert release.wait(5)
            load()
        finally:
            finished.set()

    monkeypatch.setattr(runtime, '_load', slow_load)
    monkeypatch.setattr(runtime, '_execute', lambda *_a, **_k: pytest.fail('Cancelled guest was started'))
    token = SimpleToken()
    with ThreadPoolExecutor() as executor:
        pending = executor.submit(runtime.run, [], MemoryPath(), token, Event())
        try:
            assert entered.wait(3)
            token.cancel()
            result = pending.result(timeout=1)
            assert result.killed_by_token
            assert result.returncode is None
            # Another caller waits on the same preparation, not a second compiler.
            assert runtime.run([], MemoryPath(), TimeoutToken(0.02), Event()).killed_by_token
            assert calls == ['load']
        finally:
            release.set()
            assert finished.wait(3)


@pytest.mark.parametrize('error_type', [None, RuntimeError, KeyboardInterrupt, SystemExit])
def test_condition_during_execution(wasm_home: Path, error_type: Optional[Type[BaseException]]) -> None:
    (wasm_home / 'python.wasm').write_text('(module (func (export "_start") (loop $spin br $spin)))')
    runtime = WasmRuntime(wasm_home)
    runtime._load()
    observed = Event()
    failure = error_type('condition failed') if error_type is not None else None

    def condition():
        if threading.current_thread().name == 'throng-wasi-cancellation' and not observed.is_set():
            observed.set()
            if failure is not None:
                raise failure
            return True
        return False

    token = ConditionToken(condition, caching=False, suppress_exceptions=False)
    stopped = Event()
    # Bound failure even when the broken implementation loses its watchdog.
    assert runtime._engine is not None
    guard = threading.Timer(2, runtime._engine.increment_epoch)
    guard.start()
    try:
        if error_type is not None:
            with pytest.raises(error_type, match='condition failed') as caught:
                runtime.run([], MemoryPath(), token, stopped)
            assert caught.value is failure
        else:
            result = runtime.run([], MemoryPath(), token, stopped)
            assert result.returncode == 130
            assert result.killed_by_token
        assert observed.is_set()
    finally:
        guard.cancel()
        guard.join()


@pytest.mark.parametrize('method', ['read', 'install', 'get'])
def test_non_command_operations_accept_tokens(tmp_path: Path, runtime: WasmRuntime, method: str) -> None:
    manager = WasmManager(tmp_path, runtime=runtime)
    state = manager.read()
    isolate = manager.get(state)
    token = SimpleToken(cancelled=True)
    try:
        operation = {
            'get': lambda: manager.get(state, token=token),
            'install': lambda: isolate.install('mypy', token=token),
            'read': lambda: isolate.read(token=token),
        }[method]
        with pytest.raises(CancellationError):
            operation()
    finally:
        isolate.kill()


def test_cancelled_manager_does_not_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = WasmManager(tmp_path)
    monkeypatch.setattr('throng_wasmtime.manager.snapshot', lambda *_a, **_k: pytest.fail('Snapshot started'))
    assert manager.run('python', SimpleToken(cancelled=True)).killed_by_token


def test_transient_token_remains_cancelled_for_entire_chain(tmp_path: Path, runtime: WasmRuntime) -> None:
    isolate = WasmIsolate(snapshot(tmp_path), runtime)
    checks = []

    def once():
        checks.append(True)
        return len(checks) == 1

    try:
        results = isolate.chain('python', 'python', token=ConditionToken(once, caching=False))
        assert all(isinstance(result, WasmResult) and not result.success and result.killed_by_token for result in results)
        assert len(checks) == 1
    finally:
        isolate.kill()


@pytest.mark.parametrize('operation', ['read', 'install', 'get'])
def test_non_command_mutex_wait_is_cancellable(tmp_path: Path, runtime: WasmRuntime, operation: str) -> None:
    manager = WasmManager(tmp_path, runtime=runtime)
    state = manager.read()
    isolate = manager.get(state)
    entered = Event()
    def signal_check():
        entered.set()
        return False

    token = SimpleToken(ConditionToken(signal_check))
    call = {
        'read': lambda: isolate.read(token=token),
        'install': lambda: isolate.install('mypy', token=token),
        'get': lambda: manager.get(state, token=token),
    }[operation]
    lock = manager._lock if operation == 'get' else isolate._lock
    try:
        with ThreadPoolExecutor() as executor, lock:
            pending = executor.submit(call)
            assert entered.wait(3)
            token.cancel()
            with pytest.raises(CancellationError):
                pending.result(timeout=1)
    finally:
        isolate.kill()


def test_cancel_after_compilation_never_starts_guest(runtime: WasmRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    token = SimpleToken()
    load = runtime._load

    def cancel_after_load():
        load()
        token.cancel()

    monkeypatch.setattr(runtime, '_load', cancel_after_load)
    monkeypatch.setattr(runtime, '_execute', lambda *_a, **_k: pytest.fail('Cancelled guest started'))
    result = runtime.run([], MemoryPath(), token, Event())
    assert result.killed_by_token
    assert result.returncode is None


def test_cancel_slow_host_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release, finished = Event(), Event(), Event()
    token = SimpleToken()

    def slow_snapshot(*_args, **kwargs):
        entered.set()
        try:
            assert release.wait(5)
            kwargs['check']()
        finally:
            finished.set()

    monkeypatch.setattr('throng_wasmtime.manager.snapshot', slow_snapshot)
    with ThreadPoolExecutor() as executor:
        pending = executor.submit(WasmManager(tmp_path).read, token=token)
        try:
            assert entered.wait(3)
            token.cancel()
            with pytest.raises(CancellationError):
                pending.result(timeout=1)
        finally:
            release.set()
            assert finished.wait(3)


def test_cancel_short_guest_before_watchdog_first_poll(wasm_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (wasm_home / 'python.wasm').write_text('''(module
      (import "wasi_snapshot_preview1" "sched_yield" (func $ready (result i32)))
      (memory (export "memory") 1)
      (func (export "_start") (drop (call $ready))))''')
    token = SimpleToken()
    monkeypatch.setattr('throng_wasmtime.wasi.MemoryWasi.w_sched_yield', lambda *_a: token.cancel())
    result = WasmRuntime(wasm_home).run([], MemoryPath(), token, Event())
    assert result.killed_by_token
    assert result.returncode == 130


def test_composite_tokens_preserve_original_cause(tmp_path: Path, runtime: WasmRuntime) -> None:
    cause = TimeoutToken(0)
    token = SimpleToken() + (SimpleToken() + cause)
    isolate = WasmIsolate(snapshot(tmp_path), runtime)
    try:
        assert isolate.run('python', token).killed_by_token
        with pytest.raises(TimeoutCancellationError) as caught:
            isolate.read(token=token)
        assert caught.value.token is cause
    finally:
        isolate.kill()


@pytest.mark.parametrize('method', ['read', 'restore'])
def test_cancel_mid_snapshot_is_atomic(tmp_path: Path, runtime: WasmRuntime, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    isolate = WasmIsolate(snapshot(tmp_path), runtime)
    (isolate.path / 'a').write_bytes(b'a' * 200000)
    (isolate.path / 'b').write_bytes(b'b')
    before = isolate.read()
    token = SimpleToken()
    call: Callable[[], object]
    if method == 'read':
        walk = MemoryPath.walk

        def cancel_mid_walk(path):
            for item in walk(path):
                yield item
                token.cancel()

        monkeypatch.setattr(MemoryPath, 'walk', cancel_mid_walk)
        call = lambda: isolate.read(token=token)
    else:
        read = ZipExtFile.read

        def cancel_mid_read(stream, *args, **kwargs):
            data = read(stream, *args, **kwargs)
            token.cancel()
            return data

        monkeypatch.setattr(ZipExtFile, 'read', cancel_mid_read)
        call = lambda: restore(before, isolate.path, check=token.check)
    try:
        with pytest.raises(CancellationError):
            call()
        monkeypatch.undo()
        assert isolate.read() == before
    finally:
        isolate.kill()


def test_cancel_during_wasi_setup_never_enters_guest(runtime: WasmRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    token = SimpleToken()
    link = MemoryWasi.link

    def cancel_after_link(*args, **kwargs):
        link(*args, **kwargs)
        token.cancel()

    monkeypatch.setattr(MemoryWasi, 'link', cancel_after_link)
    result = runtime.run([], MemoryPath(), token, Event())
    assert result.killed_by_token
    assert result.returncode is None
    assert runtime.run([], MemoryPath(), SimpleToken(), Event()).success


def test_token_error_while_waiting_does_not_strand_mutex(runtime: WasmRuntime) -> None:
    checking = Event()
    failure = ValueError('bad cancellation condition')

    def condition():
        checking.set()
        raise failure

    token = ConditionToken(condition, suppress_exceptions=False)
    with runtime.lock, ThreadPoolExecutor() as executor:
        pending = executor.submit(runtime.run, [], MemoryPath(), token, Event())
        assert checking.wait(3)
        with pytest.raises(ValueError, match='bad cancellation condition') as caught:
            pending.result(timeout=1)
        assert caught.value is failure
    assert runtime.run([], MemoryPath(), SimpleToken(), Event()).success


def test_suppressed_token_error_keeps_cantok_semantics(runtime: WasmRuntime) -> None:
    def fail():
        raise ValueError('suppressed')

    for default in (False, True):
        token = ConditionToken(fail, suppress_exceptions=True, default=default)
        result = runtime.run([], MemoryPath(), token, Event())
        assert result.success is not default
        assert result.killed_by_token is default


def test_preparation_failure_after_cancellation_can_be_retried(runtime: WasmRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release, finished = Event(), Event(), Event()
    load = runtime._load
    failure = OSError('cannot read runtime')

    def fail_later():
        entered.set()
        try:
            assert release.wait(3)
            raise failure
        finally:
            finished.set()

    monkeypatch.setattr(runtime, '_load', fail_later)
    token = SimpleToken()
    with ThreadPoolExecutor() as executor:
        pending = executor.submit(runtime.run, [], MemoryPath(), token, Event())
        try:
            assert entered.wait(3)
            token.cancel()
            assert pending.result(timeout=1).killed_by_token
        finally:
            release.set()
            assert finished.wait(3)
    with pytest.raises(OSError, match='cannot read runtime'):
        runtime.run([], MemoryPath(), SimpleToken(), Event())
    monkeypatch.setattr(runtime, '_load', load)
    assert runtime.run([], MemoryPath(), SimpleToken(), Event()).success


def test_prepared_module_is_reused_after_cancelled_wait(runtime: WasmRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime._load()
    module = runtime._module
    runtime._load()
    assert runtime._module is module
    monkeypatch.setattr(runtime, '_load', lambda: pytest.fail('Recompiled an already loaded module'))
    assert runtime.run([], MemoryPath(), SimpleToken(cancelled=True), Event()).killed_by_token
    assert runtime.run([], MemoryPath(), SimpleToken(), Event()).success


def test_non_cached_condition_can_be_reused_by_a_new_call(runtime: WasmRuntime) -> None:
    cancelled = Event()
    token = ConditionToken(cancelled.is_set, caching=False)
    cancelled.set()
    assert runtime.run([], MemoryPath(), token, Event()).killed_by_token
    cancelled.clear()
    assert runtime.run([], MemoryPath(), token, Event()).success


@pytest.mark.filterwarnings('ignore:CounterToken is deprecated:DeprecationWarning')
def test_counter_token_counts_checks_not_commands(runtime: WasmRuntime) -> None:
    exhausted = CounterToken(0)
    available = CounterToken(10000)
    assert runtime.run([], MemoryPath(), exhausted, Event()).killed_by_token
    assert runtime.run([], MemoryPath(), available, Event()).success


class ExternalToken(AbstractToken):
    """User token: the plugin must use check(), without guessing its kind."""

    def __init__(self) -> None:
        super().__init__()
        self.signal = Event()

    def __bool__(self) -> bool:
        raise AssertionError('The plugin must use the check() contract')

    def _superpower(self) -> bool:
        return self.signal.is_set()

    def _text_representation_of_superpower(self) -> str:
        return 'external signal'

    def _get_superpower_exception_message(self) -> str:
        return 'External signal cancelled the operation'


def test_custom_abstract_token_works_across_public_api(tmp_path: Path, runtime: WasmRuntime) -> None:
    token = ExternalToken()
    manager = WasmManager(tmp_path, runtime=runtime)
    state = manager.read(token=token)
    isolate = manager.get(state, token=token)
    try:
        assert manager.run('python', token).success
        assert isolate.run('python', token).success
        assert all(result.success for result in isolate.chain('python', token=token))
        isolate.install(token=token)
        token.signal.set()
        assert isolate.run('invalid', token).killed_by_token
        assert manager.run('invalid', token).killed_by_token
        for operation in [lambda: manager.read(token=token), lambda: manager.get(state, token=token),
                          lambda: isolate.read(token=token), lambda: isolate.install('tool', token=token),
                          lambda: WasmIsolate(state, runtime, token=token)]:
            with pytest.raises(CancellationError, match='External signal') as caught:
                operation()
            assert caught.value.token is token
    finally:
        isolate.kill()


def test_cancelling_queued_command_does_not_interrupt_running_guest(wasm_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (wasm_home / 'python.wasm').write_text('''(module
      (import "wasi_snapshot_preview1" "sched_yield" (func $ready (result i32)))
      (memory (export "memory") 1)
      (func (export "_start") (drop (call $ready)) (loop $spin br $spin)))''')
    runtime = WasmRuntime(wasm_home)
    runtime._load()
    started = Event()
    monkeypatch.setattr(MemoryWasi, 'w_sched_yield', lambda *_a: started.set())
    active_token, queued_token = SimpleToken(), ExternalToken()
    queued_seen = Event()
    check = queued_token.check

    def observed_check(**kwargs):
        check(**kwargs)
        queued_seen.set()

    monkeypatch.setattr(queued_token, 'check', observed_check)
    with ThreadPoolExecutor(max_workers=2) as executor:
        active = executor.submit(runtime.run, [], MemoryPath(), active_token, Event())
        try:
            assert started.wait(3)
            queued = executor.submit(runtime.run, [], MemoryPath(), queued_token, Event())
            assert queued_seen.wait(3)
            queued_token.signal.set()
            assert queued.result(timeout=1).killed_by_token
            assert not active.done()
        finally:
            active_token.cancel()
        assert active.result(timeout=3).returncode == 130


def test_kill_during_cold_load_does_not_leave_guest_or_block_sibling(tmp_path: Path, runtime: WasmRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release, finished = Event(), Event(), Event()
    load = runtime._load

    def blocked_load():
        entered.set()
        try:
            assert release.wait(5)
            load()
        finally:
            finished.set()

    monkeypatch.setattr(runtime, '_load', blocked_load)
    state = snapshot(tmp_path)
    isolate, sibling = WasmIsolate(state, runtime), WasmIsolate(state, runtime)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = executor.submit(isolate.run, 'python')
            try:
                assert entered.wait(3)
                executor.submit(isolate.kill).result(timeout=1)
                result = pending.result(timeout=1)
                assert result.returncode is None
                assert not result.killed_by_token
                assert not isolate.path.exists()
            finally:
                release.set()
                assert finished.wait(3)
        assert sibling.run('python').success
    finally:
        isolate.kill()
        sibling.kill()


def test_cancel_module_start_section(wasm_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (wasm_home / 'python.wasm').write_text('''(module
      (import "wasi_snapshot_preview1" "sched_yield" (func $ready (result i32)))
      (memory (export "memory") 1)
      (func $initialize (drop (call $ready)) (loop $spin br $spin)) (start $initialize)
      (func (export "_start")))''')
    runtime = WasmRuntime(wasm_home)
    runtime._load()
    assert runtime._engine is not None
    interrupt = runtime._engine.increment_epoch
    forced = Event()
    token = SimpleToken()

    def force_interrupt():
        forced.set()
        interrupt()

    guard = threading.Timer(3, force_interrupt)

    def cancel_from_start(*_args):
        # Start both cancellation and the failsafe only after guest initialization begins.
        guard.start()
        token.cancel()

    monkeypatch.setattr(MemoryWasi, 'w_sched_yield', cancel_from_start)
    try:
        result = runtime.run([], MemoryPath(), token, Event())
        assert result.returncode == 130
        assert result.killed_by_token
        assert not forced.is_set(), 'Cancellation watchdog did not interrupt module initialization'
    finally:
        guard.cancel()
        if guard.ident is not None:
            guard.join()


def test_cancel_inside_snapshot_file_is_atomic(tmp_path: Path, runtime: WasmRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate = WasmIsolate(snapshot(tmp_path), runtime)
    (isolate.path / 'large').write_bytes(b'x' * 200000)
    before = isolate.read()
    token = SimpleToken()
    with zipfile.ZipFile(BytesIO(), 'w') as archive, archive.open('probe', 'w') as writer:
        writer_type = type(writer)
    original = writer_type.write
    chunks = []

    def cancel_after_chunk(writer, data):
        result = original(writer, data)
        chunks.append(len(data))
        token.cancel()
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(writer_type, 'write', cancel_after_chunk)
            with pytest.raises(CancellationError):
                isolate.read(token=token)
        assert len(chunks) == 1
        assert 0 < chunks[0] < 200000
        assert isolate.read() == before
    finally:
        isolate.kill()
