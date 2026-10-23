"""A reusable CPython module, with a fresh WASI store for every command."""

import os
import shlex
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from pkgutil import get_data
from threading import Event, Lock, Thread
from typing import Iterator, List, Optional, Sequence, Type, Union, cast
from weakref import finalize

import wasmtime
from cantok import AbstractToken, CancellationError

from throng_wasm.bootstrap import ensure_bundle
from throng_wasm.cancellation import (
    POLL_INTERVAL,
    Background,
    Cancellation,
    StoppedError,
)
from throng_wasm.memory import MemoryPath, Node
from throng_wasm.state import restore, snapshot
from throng_wasm.wasi import MemoryWasi


@dataclass
class WasmResult:
    success: bool
    returncode: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    killed_by_token: bool = False


@contextmanager
def _watch_cancellation(cancellation: Cancellation, engine: wasmtime.Engine, interrupted: Event) -> Iterator[None]:
    """Stop and join the observer before the store or runtime mutex is released."""
    finished = Event()

    def watch() -> None:
        while not finished.wait(POLL_INTERVAL):
            try:
                cancellation.check()
            except BaseException:  # noqa: BLE001 -- re-raised on the caller after stopping WASM
                interrupted.set()
                engine.increment_epoch()
                return

    watcher = Thread(target=watch, name='throng-wasi-cancellation', daemon=True)
    watcher.start()
    try:
        yield
    finally:
        finished.set()
        watcher.join()


def command_arguments(command: str) -> List[str]:
    """Parse argv, never a host shell. CPython implements its own CLI options."""
    args = shlex.split(command)
    if not args:
        raise ValueError('The command is empty.')
    executable = args.pop(0)
    if executable in {'python', 'python3'}:
        return args
    if executable in {'mypy', 'pyflakes', 'pycodestyle', 'flake8'}:
        return ['-m', executable, *args]
    if executable == 'ls':
        source = get_data('throng_wasm', 'commands.py')
        if source is None:
            raise FileNotFoundError('Missing package resource: commands.py')
        return ['-P', '-c', source.decode('utf-8') + '\nraise SystemExit(list_directory(sys.argv[1:]))', *args]
    if executable == 'ruff':
        raise ValueError('Ruff is a native Rust executable, not a CPython WASI module. Use mypy or pyflakes; Ruff needs a separate WASI port.')
    raise ValueError(f'Unsupported command: {executable!r}. Use python -m MODULE or python SCRIPT.')


class WasmRuntime:
    """A reusable module with private memory-only WASI state on every command."""

    def __init__(
        self, home: Optional[Union[str, Path]] = None, *,
        packages: Sequence[Union[str, Path]] = (),
        memory_limit: int = 512 * 1024 * 1024, cache: bool = False,
    ) -> None:
        if memory_limit <= 0:
            raise ValueError('memory_limit must be positive.')
        if cache:
            raise ValueError('Disk compilation caches are disabled; reuse a WasmRuntime to cache its module in memory.')
        self.home = Path(home).resolve() if home is not None else None
        self.packages = tuple(Path(path).resolve() for path in packages)
        self.memory_limit = memory_limit
        self.lock = Lock()
        self._loading: Optional[Background[None]] = None
        self._resources = ExitStack()
        finalize(self, self._resources.close)
        self._image: Optional[MemoryPath] = None
        self._libraries: List[MemoryPath] = []
        self._engine: Optional[wasmtime.Engine] = None
        self._module: Optional[wasmtime.Module] = None

    @classmethod
    def from_environment(cls) -> 'WasmRuntime':
        paths = [part for part in os.environ.get('THRONG_WASM_PACKAGES', '').split(os.pathsep) if part]
        return cls(os.environ.get('THRONG_WASM_HOME') or None, packages=paths)

    def _load(self) -> None:
        if self._module is not None:
            return
        if self.home is None:
            image = ensure_bundle()
        else:
            if not (self.home / 'lib').is_dir():
                raise FileNotFoundError(f'WASI library directory does not exist: {self.home / "lib"}')
            image = restore(snapshot(self.home))
        libraries = [restore(snapshot(path)) for path in self.packages]
        config = wasmtime.Config()
        config.epoch_interruption = True
        engine = wasmtime.Engine(config)
        try:
            module = wasmtime.Module(engine, (image / 'python.wasm').read_bytes())
        except BaseException:
            engine.close()
            raise
        self._resources.callback(engine.close)
        self._resources.callback(module.close)
        self._engine, self._module = engine, module
        self._image, self._libraries = image, libraries

    def run(self, args: List[str], path: MemoryPath, token: AbstractToken, stopped: Event, *,  # noqa: PLR0913
            packages: Sequence[MemoryPath] = (), scripts: Optional[MemoryPath] = None) -> WasmResult:
        return self._run(args, path, Cancellation(token, stopped), packages=packages, scripts=scripts)

    def _run(self, args: List[str], path: MemoryPath, cancellation: Cancellation, *,
             packages: Sequence[MemoryPath] = (), scripts: Optional[MemoryPath] = None) -> WasmResult:
        try:
            with cancellation.hold(self.lock):
                # A cancelled caller leaves at most one shared compilation running.
                # Its output is immutable and cannot execute or modify an isolate.
                if self._loading is None and self._module is None:
                    self._loading = Background(self._load)
                if self._loading is not None:
                    try:
                        cancellation.wait(self._loading)
                    except BaseException:
                        if self._loading.done.is_set():
                            self._loading = None
                        raise
                    self._loading = None
                cancellation.check()
                assert self._engine is not None
                assert self._module is not None
                return self._execute(args, path, cancellation, self._engine, self._module, packages, scripts)
        except (CancellationError, StoppedError) as exception:
            return WasmResult(False, killed_by_token=isinstance(exception, CancellationError))

    def _prepare_wasi(self, args: List[str], path: MemoryPath, cancellation: Cancellation,
                      packages: Sequence[MemoryPath], scripts: Optional[MemoryPath]) -> MemoryWasi:
        assert self._image is not None
        env = [
            ('PYTHONHOME', '/python'),
            ('PYTHONPATH', ':'.join(f'/packages/{index}' for index in range(len(packages) + len(self._libraries)))),
            ('PYTHONDONTWRITEBYTECODE', '1'), ('PYTHONUNBUFFERED', '1'),
            ('PYTHONHASHSEED', '0'), ('TMPDIR', '/tmp'),
        ]
        mounts = [('/', path, False), ('/python', self._image, True)]
        mounts.extend((f'/packages/{index}', package, False) for index, package in enumerate(packages))
        mounts.extend((f'/packages/{index}', package, True) for index, package in enumerate(self._libraries, start=len(packages)))
        if scripts is not None:
            mounts.append(('/scripts', scripts, False))
        devices = MemoryPath()
        devices.node.children['null'] = Node(null=True)
        mounts.extend([('/tmp', MemoryPath(), False), ('/dev', devices, False)])
        return MemoryWasi(['/python/python.wasm', *args], env, mounts, Event(), self.memory_limit, check=cancellation.check)

    def _execute(  # noqa: PLR0913
        self, args: List[str], path: MemoryPath, cancellation: Cancellation,
        engine: wasmtime.Engine, module: wasmtime.Module, packages: Sequence[MemoryPath], scripts: Optional[MemoryPath],
    ) -> WasmResult:
        with ExitStack() as resources:
            store, linker = wasmtime.Store(engine), wasmtime.Linker(engine)
            resources.callback(store.close)
            resources.callback(linker.close)
            wasi = self._prepare_wasi(args, path, cancellation, packages, scripts)
            store.set_wasi(wasmtime.WasiConfig())
            linker.define_wasi()
            linker.allow_shadowing = True
            wasi.link(linker, module)
            store.set_limits(memory_size=self.memory_limit)
            store.set_epoch_deadline(1)
            cancellation.check()
            code, error = 0, ''
            with _watch_cancellation(cancellation, engine, wasi.stopped):
                try:
                    cancellation.check()
                    start = cast(object, linker.instantiate(store, module).exports(store)['_start'])
                    if not isinstance(start, cast(Type[object], wasmtime.Func)):
                        raise TypeError('The WASI module must export a _start function.')
                    cast(object, cast(wasmtime.Func, start)(store))
                except wasmtime.ExitTrap as exception:
                    code = exception.code
                except wasmtime.Trap as exception:
                    code, error = 1, str(exception)
            try:
                cancellation.check()
            except (CancellationError, StoppedError):
                code = 130
            return WasmResult(
                success=code == 0, returncode=code,
                stdout=wasi.stdout.data.decode('utf-8', errors='replace'),
                stderr=wasi.stderr.data.decode('utf-8', errors='replace') + error,
                killed_by_token=isinstance(cancellation.error, CancellationError),
            )
