"""Throng 0.0.10 contracts and WASM's stronger cancellation guarantees."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from cantok import CancellationError, ConditionToken, SimpleToken
from throng import AbstractIsolate, AbstractManager, throng
from throng.errors import (
    ArgumentsRedefinitionError,
    InterruptedChainError,
    InterruptedInstallationError,
    NotSuccessfulRunError,
    PreparationCommandFailedError,
)

from throng_wasm import WasmIsolate, WasmManager, WasmResult, WasmRuntime


@pytest.fixture
def managed(tmp_path: Path, runtime: WasmRuntime, monkeypatch: pytest.MonkeyPatch):
    manager = WasmManager(tmp_path, runtime=runtime)
    created = []
    create = manager._get

    def record(*args, **kwargs):
        isolate = create(*args, **kwargs)
        created.append(isolate)
        return isolate

    monkeypatch.setattr(manager, '_get', record)
    yield manager, created
    for isolate in created:
        isolate.kill()


@pytest.mark.parametrize('positional', [False, True])
def test_slot_forwards_setup_lazily(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, positional: bool) -> None:
    monkeypatch.setattr(WasmManager, 'read', lambda *_a, **_k: pytest.fail('Eager snapshot'))
    monkeypatch.setattr(WasmIsolate, 'install', lambda *_a, **_k: pytest.fail('Eager installation'))
    prepare, packages = ['python -c pass'], ['example']
    managers = (throng(tmp_path, [], prepare, packages) if positional
                else throng(tmp_path, exclude=[], prepare=prepare, packages=packages))
    manager = managers['wasm']
    assert isinstance(manager, WasmManager)
    assert manager.prepare == prepare
    assert manager.packages == packages
    assert manager.runtime is None
    assert manager.exclude == []


def test_hooks_and_positional_token_match_base_contract(managed) -> None:
    manager, created = managed
    token = SimpleToken()
    state = manager.read()
    for isolate in [manager.get(state, token), AbstractManager.get(manager, state, token)]:
        assert isinstance(isolate, WasmIsolate)
        assert AbstractIsolate.run(isolate, 'python', token, True).success
    assert len(created) == 2


@pytest.mark.parametrize('owner', ['manager', 'isolate'])
@pytest.mark.parametrize('method', ['run', 'chain'])
@pytest.mark.parametrize('policy', [False, True, LookupError, LookupError('custom'), KeyboardInterrupt, KeyboardInterrupt('custom')])
def test_failure_policy_preserves_result_and_stops_chain(managed, monkeypatch, owner, method, policy):
    manager, created = managed
    calls = []
    failure = WasmResult(False, 7, 'output', 'diagnostic')

    def execute(_self, command, cancellation):
        cancellation.check()
        calls.append(command)
        return failure if command == 'bad' else WasmResult(True, 0)

    monkeypatch.setattr(WasmIsolate, '_execute', execute)
    runner = manager if owner == 'manager' else manager.get(manager.read())
    commands = ('bad', 'after') if method == 'chain' else ('bad',)
    if policy is False:
        result = getattr(runner, method)(*commands, exception=policy)
        assert (result[0] if method == 'chain' else result) is failure
        assert calls == list(commands)
    else:
        error_type = NotSuccessfulRunError if policy is True else type(policy) if isinstance(policy, BaseException) else policy
        with pytest.raises(error_type) as caught:
            getattr(runner, method)(*commands, exception=policy)
        assert calls == ['bad']
        if policy is True:
            assert caught.value.result is failure
        elif isinstance(policy, BaseException):
            assert caught.value is policy
    assert created[-1].path.exists() == (owner == 'isolate')
    # The policy is per invocation; a later call still returns failed results.
    assert not runner.run('bad').success


@pytest.mark.parametrize('owner', ['manager', 'isolate'])
@pytest.mark.parametrize('method', ['run', 'chain'])
@pytest.mark.parametrize('policy', [False, True, LookupError, LookupError('cancelled')])
def test_cancel_before_execution_applies_policy(managed, owner, method, policy):
    manager, created = managed
    runner = manager if owner == 'manager' else manager.get(manager.read())
    options = {'token': SimpleToken(cancelled=True), 'exception': policy}
    if policy is False:
        result = getattr(runner, method)('unsupported', **options)
        result = result[0] if method == 'chain' else result
        assert result.killed_by_token
        assert result.returncode is None
    else:
        default = InterruptedChainError if method == 'chain' else NotSuccessfulRunError
        error_type = default if policy is True else type(policy) if isinstance(policy, BaseException) else policy
        with pytest.raises(error_type) as caught:
            getattr(runner, method)('unsupported', **options)
        if isinstance(policy, BaseException):
            assert caught.value is policy
    if owner == 'manager':
        assert created == []


@pytest.mark.parametrize('access', ['get', 'scope', 'run', 'chain'])
@pytest.mark.parametrize('stage', ['install', 'prepare'])
@pytest.mark.parametrize('error', [ValueError('failed'), KeyboardInterrupt('interrupt'), SystemExit(2)])
def test_setup_failure_cleans_up_and_preserves_cause(managed, monkeypatch, access, stage, error):
    manager, created = managed
    manager.packages = ['example']
    manager.prepare = ['setup', 'never']
    calls = []

    def install(_self, *packages, token):
        token.check()
        calls.append(('install', packages))
        if stage == 'install':
            raise error

    def execute(_self, command, cancellation):
        cancellation.check()
        calls.append(('command', command))
        raise error

    monkeypatch.setattr(WasmIsolate, 'install', install)
    monkeypatch.setattr(WasmIsolate, '_execute', execute)
    wrapper = InterruptedInstallationError if stage == 'install' else PreparationCommandFailedError
    expected = wrapper if isinstance(error, Exception) else type(error)
    def invoke():
        if access == 'get':
            manager.get(manager.read())
        elif access == 'scope':
            with manager.scope:
                pytest.fail('An unprepared isolate escaped')
        else:
            getattr(manager, access)('user', exception=False)

    with pytest.raises(expected) as caught:
        invoke()
    assert calls == ([('install', ('example',))] + ([('command', 'setup')] if stage == 'prepare' else []))
    assert len(created) == 1
    assert not created[0].path.exists()
    assert (caught.value.__cause__ if isinstance(error, Exception) else caught.value) is error


def test_failed_preparation_result_always_raises(managed, monkeypatch):
    manager, created = managed
    manager.prepare = ['bad', 'never']
    failed = WasmResult(False, 3, 'output', 'error')
    monkeypatch.setattr(WasmIsolate, '_execute', lambda *_a: failed)
    with pytest.raises(PreparationCommandFailedError) as caught:
        manager.run('user', exception=LookupError)
    assert isinstance(caught.value.__cause__, NotSuccessfulRunError)
    assert caught.value.__cause__.result is failed
    assert not created[0].path.exists()


@pytest.mark.parametrize('stage', ['install', 'prepare'])
@pytest.mark.parametrize('access', ['get', 'scope', 'run', 'chain'])
def test_setup_cancellation_is_latched_and_wrapped(managed, monkeypatch, stage, access):
    manager, created = managed
    manager.packages = ['example']
    manager.prepare = ['setup', 'never']
    cancel = Event()
    checks = []

    def once():
        checks.append(True)
        signalled = cancel.is_set()
        cancel.clear()
        return signalled

    token = ConditionToken(once, caching=False)

    def install(_self, *_packages, token):
        if stage == 'install':
            cancel.set()
            token.check()

    def execute(_self, _command, cancellation):
        cancel.set()
        try:
            cancellation.check()
        except CancellationError:
            return WasmResult(False, killed_by_token=True)
        pytest.fail('Cancellation was lost')

    monkeypatch.setattr(WasmIsolate, 'install', install)
    monkeypatch.setattr(WasmIsolate, '_execute', execute)
    expected = InterruptedInstallationError if stage == 'install' else PreparationCommandFailedError
    def invoke():
        if access == 'get':
            manager.get(manager.read(), token)
        elif access == 'scope':
            with manager.scope(token):
                pytest.fail('Cancelled setup escaped')
        else:
            getattr(manager, access)('user', token=token)

    with pytest.raises(expected) as caught:
        invoke()
    assert not created[0].path.exists()
    if stage == 'install':
        assert isinstance(caught.value.__cause__, CancellationError)
        assert caught.value.__cause__.token is token
    else:
        assert isinstance(caught.value.__cause__, NotSuccessfulRunError)
        assert caught.value.__cause__.result.killed_by_token
    assert checks


def test_scope_token_cancels_snapshot_and_is_not_a_command_default(managed, monkeypatch):
    manager, created = managed
    cancelled = SimpleToken(cancelled=True)
    with monkeypatch.context() as patch:
        patch.setattr('throng_wasm.manager.snapshot', lambda *_a, **_k: pytest.fail('Snapshot started'))
        with pytest.raises(CancellationError), manager.scope(token=cancelled):
            pytest.fail('Cancelled scope entered')
    assert created == []
    token = SimpleToken()
    scope = manager.scope(token)
    with pytest.raises(ArgumentsRedefinitionError):
        scope(token)
    with scope as isolate:
        token.cancel()
        assert isolate.run('python').success
        assert isolate.run('python', token).killed_by_token
    assert not isolate.path.exists()
    with manager.scope() as fresh:
        assert fresh.run('python').success


def test_scope_snapshot_wait_is_cancellable(managed, monkeypatch):
    manager, created = managed
    entered, release, finished = Event(), Event(), Event()
    token = SimpleToken()

    def slow_snapshot(*_args, check, **_kwargs):
        entered.set()
        try:
            assert release.wait(5)
            check()
        finally:
            finished.set()

    monkeypatch.setattr('throng_wasm.manager.snapshot', slow_snapshot)
    with ThreadPoolExecutor() as executor:
        future = executor.submit(manager.scope(token).__enter__)
        try:
            assert entered.wait(3)
            token.cancel()
            with pytest.raises(CancellationError) as caught:
                future.result(timeout=1)
            assert caught.value.token is token
            assert created == []
        finally:
            release.set()
            assert finished.wait(3)


@pytest.mark.parametrize('policy', [False, True])
def test_kill_between_chain_commands_preserves_lifecycle(managed, monkeypatch, policy):
    manager, _created = managed
    isolate = manager.get(manager.read())

    def execute(_command, _cancellation):
        isolate.kill()
        return WasmResult(True, 0)

    monkeypatch.setattr(isolate, '_execute', execute)
    if policy:
        with pytest.raises(NotSuccessfulRunError) as caught:
            isolate.chain('first', 'second', exception=True)
        assert not caught.value.result.killed_by_token
    else:
        first, second = isolate.chain('first', 'second')
        assert first.success
        assert not second.success
        assert not second.killed_by_token


def test_empty_chain_does_not_poll_isolate_token(managed):
    manager, _created = managed
    isolate = manager.get(manager.read())
    token = ConditionToken(lambda: pytest.fail('Empty chain polled the token'))
    assert isolate.chain(token=token, exception=True) == []
    assert manager.chain(token=SimpleToken(cancelled=True), exception=True) == []


@pytest.mark.parametrize('owner', ['manager', 'isolate'])
@pytest.mark.parametrize('policy', [False, True, LookupError, LookupError('between commands')])
def test_cancellation_between_commands_skips_remaining_work(managed, monkeypatch, owner, policy):
    manager, _created = managed
    runner = manager if owner == 'manager' else manager.get(manager.read())
    token = SimpleToken()
    calls = []

    def execute(_self, command, cancellation):
        cancellation.check()
        calls.append(command)
        token.cancel()
        return WasmResult(True, 0)

    monkeypatch.setattr(WasmIsolate, '_execute', execute)
    if policy is False:
        first, second = runner.chain('first', 'second', token=token)
        assert first.success
        assert not second.success
        assert second.killed_by_token
    else:
        expected = InterruptedChainError if policy is True else type(policy) if isinstance(policy, BaseException) else policy
        with pytest.raises(expected) as caught:
            runner.chain('first', 'second', token=token, exception=policy)
        if isinstance(policy, BaseException):
            assert caught.value is policy
    assert calls == ['first']
