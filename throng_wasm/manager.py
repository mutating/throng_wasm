"""Throng manager and isolate lifecycle."""

import json
import shlex
from pathlib import Path
from threading import Event, RLock
from typing import Dict, List, Optional, Sequence, Type, Union, cast
from uuid import uuid4

from cantok import AbstractToken, CancellationError, DefaultToken
from throng import AbstractIsolate, AbstractManager
from throng.abstracts.abstract_manager import ContextIsolateManager
from throng.abstracts.meta import Benchmarks, IsolateResourcesDescription
from throng.abstracts.results import RunResultProtocol
from throng.errors import (
    CannotInstallDependencyError,
    InterruptedChainError,
    NotSuccessfulRunError,
)

from throng_wasm.cancellation import Cancellation, StoppedError
from throng_wasm.installer import ENVIRONMENT_DIRECTORY, install_packages
from throng_wasm.memory import MemoryPath
from throng_wasm.runtime import WasmResult, WasmRuntime, command_arguments
from throng_wasm.state import DEFAULT_EXCLUDE, restore, snapshot

ExceptionPolicy = Union[bool, BaseException, Type[BaseException]]


def _raise_requested(exception: ExceptionPolicy, default: BaseException) -> None:
    if exception is True:
        raise default
    if isinstance(exception, BaseException):
        raise exception
    if exception is not False:
        raise exception(str(default))


def _skipped(command: str, exception: ExceptionPolicy) -> WasmResult:
    _raise_requested(exception, InterruptedChainError(
        f'The series of commands was interrupted before executing command {command!r} due to receiving a signal from the cancellation token.',
    ))
    return WasmResult(False, killed_by_token=True)


def _checked_result(command: str, result: WasmResult, exception: ExceptionPolicy) -> WasmResult:
    if not result.success:
        _raise_requested(exception, NotSuccessfulRunError(
            f'Command {command!r} terminated prematurely, return code {result.returncode}.', result,
        ))
    return result


GUEST_ENVIRONMENT_SCRIPT = '''
import json
import sys

version = sys.version.split()[0]
print(json.dumps({
    'implementation_name': 'cpython',
    'implementation_version': version,
    'os_name': 'posix',
    'platform_machine': 'wasm32',
    'platform_release': '',
    'platform_system': 'WASI',
    'platform_version': '',
    'python_full_version': version,
    'platform_python_implementation': 'CPython',
    'python_version': '.'.join(map(str, sys.version_info[:2])),
    'sys_platform': 'wasi',
}))
'''


class WasmIsolate(AbstractIsolate):
    def __init__(self, state: bytes, runtime: Optional[WasmRuntime] = None, *,
                 exclude: Sequence[str] = (),
                 token: AbstractToken = DefaultToken(), _cancellation: Optional[Cancellation] = None) -> None:  # noqa: B008
        self._lock = RLock()
        self._stopped = Event()
        self.id = uuid4().hex
        self._root = MemoryPath()
        self.path = self._root / 'project'
        self._environment = self._root / ENVIRONMENT_DIRECTORY
        self.runtime = runtime
        self.exclude = tuple(exclude)
        cancellation = _cancellation if _cancellation is not None else Cancellation(token)
        try:
            cancellation.check()
            self.path.mkdir()
            restore(state, self.path, check=cancellation.check)
            saved = self.path / ENVIRONMENT_DIRECTORY
            if saved.exists():
                saved.replace(self._environment)
        except BaseException:
            self.kill()
            raise

    def get_id(self) -> str:
        return self.id

    def get_benchmarks(self) -> Benchmarks:
        return Benchmarks(single_core=None, multi_core=None)

    def get_resources_description(self) -> IsolateResourcesDescription:
        return IsolateResourcesDescription(shared=True, shared_with=None)

    def _check_alive(self) -> None:
        if self._stopped.is_set():
            raise RuntimeError('The WASM isolate has been destroyed.')

    def run(self, command: str, token: AbstractToken = DefaultToken(), exception: ExceptionPolicy = False) -> WasmResult:  # noqa: B008
        return cast(WasmResult, super().run(command, token=token, exception=exception))

    def _run(self, command: str, token: AbstractToken = DefaultToken()) -> WasmResult:  # noqa: B008
        self._check_alive()
        return self._execute(command, Cancellation(token, self._stopped))

    def _execute(self, command: str, cancellation: Cancellation) -> WasmResult:
        try:
            with cancellation.hold(self._lock):
                args = command_arguments(command)
                if self.runtime is None:
                    self.runtime = WasmRuntime.from_environment()
                packages = self._environment / 'packages'
                scripts = self._environment / 'scripts'
                return self.runtime._run(args, self.path, cancellation,
                                         packages=[packages] if packages.is_dir() else (),
                                         scripts=scripts if scripts.is_dir() else None)
        except (CancellationError, StoppedError) as exception:
            return WasmResult(False, killed_by_token=isinstance(exception, CancellationError))

    def chain(self, *commands: str, token: AbstractToken = DefaultToken(), exception: ExceptionPolicy = False) -> List[RunResultProtocol]:  # noqa: B008
        self._check_alive()
        cancellation = Cancellation(token, self._stopped)
        results: List[RunResultProtocol] = []
        for command in commands:
            try:
                cancellation.check()
            except CancellationError:
                results.append(_skipped(command, exception))
            except StoppedError:
                results.append(_checked_result(command, WasmResult(False), exception))
            else:
                results.append(_checked_result(command, self._execute(command, cancellation), exception))
        return results

    def read(self, *, token: AbstractToken = DefaultToken()) -> bytes:  # noqa: B008
        self._check_alive()
        cancellation = Cancellation(token, self._stopped)
        with cancellation.hold(self._lock):
            return snapshot(self.path, self.exclude, extra=[self._environment], check=cancellation.check)

    def install(self, *packages: str, token: AbstractToken = DefaultToken()) -> None:  # noqa: B008
        self._check_alive()
        cancellation = Cancellation(token, self._stopped)
        try:
            with cancellation.hold(self._lock):
                if not packages:
                    return
                self._install(packages, cancellation)
        except Exception as exception:
            # Token callback errors and cantok's precise cancellation cause belong
            # to the caller; they are not failures to resolve a package.
            if cancellation.error is exception and not isinstance(exception, StoppedError):
                raise
            raise CannotInstallDependencyError(f'Cannot install {", ".join(repr(package) for package in packages)}: {exception}') from exception

    def _install(self, packages: Sequence[str], cancellation: Cancellation) -> None:
        # Query the guest, never resolve against the host's Python version.
        result = self._execute('python -P -c ' + shlex.quote(GUEST_ENVIRONMENT_SCRIPT), cancellation)
        cancellation.check()
        if not result.success or result.stdout is None:
            raise RuntimeError(f'Cannot inspect the guest interpreter: {result.stderr}')
        environment = cast(Dict[str, str], json.loads(result.stdout))
        install_packages(self._root, packages, environment, self._check_alive, cancellation=cancellation)

    def kill(self) -> None:
        self._stopped.set()
        with self._lock:
            self._root.root.children.clear()
            self._root.root.closed = True


class WasmScope(ContextIsolateManager):
    """Include snapshot reads in setup cancellation without binding body commands."""

    manager: 'WasmManager'

    def __call__(self, token: AbstractToken = DefaultToken()) -> 'WasmScope':  # noqa: B008
        super().__call__(token)
        return self

    def __enter__(self) -> WasmIsolate:
        token = Cancellation(DefaultToken() if self.token is None else self.token).as_token()
        isolate = self.manager._open(token)
        self.isolate = isolate
        return isolate


class WasmManager(AbstractManager):
    def __init__(
        self, path: Optional[Union[str, Path]] = None, exclude: Optional[Sequence[str]] = None,
        prepare: Optional[List[str]] = None, packages: Optional[List[str]] = None, *,
        runtime: Optional[WasmRuntime] = None,
    ) -> None:
        super().__init__(Path.cwd() if path is None else path, None if exclude is None else list(exclude), prepare, packages)
        self.runtime = runtime
        self._lock = RLock()

    @property
    def scope(self) -> WasmScope:
        return WasmScope(self)

    def _open(self, token: AbstractToken) -> WasmIsolate:
        return self.get(self.read(token=token), token=token)

    def read(self, *, token: AbstractToken = DefaultToken()) -> bytes:  # noqa: B008
        return self._read(Cancellation(token))

    def _read(self, cancellation: Cancellation) -> bytes:
        # Host filesystem reads may block. The worker only reads host files and
        # owns its archive; cancellation never leaves it accessing an isolate.
        exclude = DEFAULT_EXCLUDE if self.exclude is None else tuple(self.exclude)
        return cancellation.call(lambda: snapshot(self.path, exclude, check=cancellation.check))

    def get(self, state: bytes, token: AbstractToken = DefaultToken()) -> WasmIsolate:  # noqa: B008
        return cast(WasmIsolate, super().get(state, token=Cancellation(token).as_token()))

    def _get(self, state: bytes, token: AbstractToken = DefaultToken()) -> WasmIsolate:  # noqa: B008
        cancellation = Cancellation(token)
        with cancellation.hold(self._lock):
            if self.runtime is None:
                self.runtime = WasmRuntime.from_environment()
            exclude = DEFAULT_EXCLUDE if self.exclude is None else self.exclude
            return WasmIsolate(state, self.runtime, exclude=exclude, _cancellation=cancellation)

    def run(self, command: str, token: AbstractToken = DefaultToken(), exception: ExceptionPolicy = False) -> WasmResult:  # noqa: B008
        shared_token = Cancellation(token).as_token()
        try:
            isolate = self._open(shared_token)
        except CancellationError:
            return _checked_result(command, WasmResult(False, killed_by_token=True), exception)
        try:
            return isolate.run(command, token=shared_token, exception=exception)
        finally:
            isolate.kill()

    def chain(self, *commands: str, token: AbstractToken = DefaultToken(), exception: ExceptionPolicy = False) -> List[RunResultProtocol]:  # noqa: B008
        shared_token = Cancellation(token).as_token()
        try:
            isolate = self._open(shared_token)
        except CancellationError:
            return [_skipped(command, exception) for command in commands]
        try:
            return isolate.chain(*commands, token=shared_token, exception=exception)
        finally:
            isolate.kill()
