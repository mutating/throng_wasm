"""Reusable microbenchmark scenarios; prepare resources before starting timers."""

import os
import shlex
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import partial
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, Iterator, List, Optional, Sequence, Union
from zipfile import ZipFile

from microbenchmark import Scenario, ScenarioGroup

from throng_wasmtime.manager import WasmIsolate, WasmManager
from throng_wasmtime.runtime import WasmRuntime
from throng_wasmtime.state import snapshot

ITERATIONS = 10
COLD_ITERATIONS = 3
FILE_COUNTS = (1, 100)
PACKAGES = ('mypy==1.14.1', 'pyflakes==3.3.2')
SOURCE = (
    'from typing import Iterable\n\n'
    'def total(values: Iterable[int]) -> int:\n'
    '    return sum(values)\n\n'
    'answer: int = total([1, 2, 3])\n'
)
LINTER_ARGUMENTS = {
    'pyflakes': ('-m', 'pyflakes', '.'),
    'mypy': ('-m', 'mypy', '--no-incremental', '--cache-dir=/dev/null', '--no-site-packages', '--python-version=3.13', '--platform=linux', '.'),
    'mypy_incremental': ('-m', 'mypy', '--cache-dir=.mypy_cache', '--no-site-packages', '--python-version=3.13', '--platform=linux', '.'),
}
SCENARIO_NAMES = (
    'startup.native', 'startup.wasm_reused', 'startup.wasm_new_isolate', 'startup.wasm_new_runtime_memory',
    *(f'{tool}.{count}_files.wasm_reused' for count in FILE_COUNTS for tool in LINTER_ARGUMENTS),
)


def checked_wasm(runner: Union[WasmIsolate, WasmManager], command: str) -> None:
    result = runner.run(command)
    if not result.success:
        raise RuntimeError(f'{command}: {result.stdout}\n{result.stderr}')


def checked_native(python: str, args: Sequence[str], directory: Path, env: Dict[str, str]) -> None:
    result = subprocess.run([python, *args], cwd=directory, env=env, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f'{list(args)}: {result.stdout}\n{result.stderr}')


def _cold_runtime(project: Path, runtime: WasmRuntime) -> None:
    cold = WasmRuntime(runtime.home, packages=runtime.packages, memory_limit=runtime.memory_limit)
    checked_wasm(WasmManager(project, runtime=cold), 'python -c pass')


@dataclass
class BenchmarkSuite:
    """Prepared workloads, owned by the surrounding prepare() context."""

    projects: Dict[int, Path]
    isolates: Dict[int, WasmIsolate]
    scenarios: Dict[str, Scenario]
    native_env: Dict[str, str]
    pure_env: Dict[str, str]
    all: ScenarioGroup


@contextmanager
def prepare(*, native_python: Optional[str] = None, number: int = ITERATIONS,
            cold_number: int = COLD_ITERATIONS) -> Iterator[BenchmarkSuite]:
    """Prepare ten default scenarios, or all 22 comparisons with a native linter environment.

    Downloads, corpus creation and package installation happen before yielding.
    Use scenario.run(warmup=2) for steady-state measurements, and warmup=0 for
    startup.wasm_new_runtime_memory. Each command still gets a fresh interpreter.
    """
    if number < 1 or cold_number < 1:
        raise ValueError('Benchmark iteration counts must be positive.')
    runtime = WasmRuntime.from_environment()
    python = str(Path(native_python or sys.executable).absolute())
    native_env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONHASHSEED='0', PYTHONUNBUFFERED='1')
    for key in ('PYTHONPATH', 'PYTHONHOME', 'COVERAGE_PROCESS_START'):
        native_env.pop(key, None)
    with TemporaryDirectory(prefix='throng-wasi-benchmark-') as temporary, ExitStack() as stack:
        base = Path(temporary)
        projects: Dict[int, Path] = {}
        isolates: Dict[int, WasmIsolate] = {}
        for count in FILE_COUNTS:
            project = base / str(count)
            project.mkdir()
            for index in range(count):
                (project / f'module_{index:03}.py').write_text(SOURCE)
            manager = WasmManager(project, runtime=runtime)
            isolate = manager.get(manager.read())
            stack.callback(isolate.kill)
            isolate.install(*PACKAGES)
            projects[count], isolates[count] = project, isolate

        small = projects[1]
        scenarios = {
            'startup.native': Scenario(partial(checked_native, python, ('-c', 'pass'), small, native_env),
                                       name='startup.native', doc='Start a native Python subprocess.', number=number),
            'startup.wasm_reused': Scenario(partial(checked_wasm, isolates[1], 'python -c pass'),
                                            name='startup.wasm_reused', doc='Start CPython in a prepared WASM isolate.', number=number),
            'startup.wasm_new_isolate': Scenario(partial(checked_wasm, WasmManager(small, runtime=runtime), 'python -c pass'),
                                                name='startup.wasm_new_isolate', doc='Snapshot a project and create, run and destroy its isolate.', number=number),
            'startup.wasm_new_runtime_memory': Scenario(partial(_cold_runtime, small, runtime),
                                                       name='startup.wasm_new_runtime_memory', doc='Compile a fresh runtime in memory and run a new isolate.', number=cold_number),
        }
        for count, isolate in isolates.items():
            for tool, arguments in LINTER_ARGUMENTS.items():
                name = f'{tool}.{count}_files.wasm_reused'
                scenarios[name] = Scenario(partial(checked_wasm, isolate, shlex.join(['python', *arguments])),
                                           name=name, doc=f'Run {tool} on {count} files in WASI.', number=number)

        pure_env: Dict[str, str] = {}
        if native_python is not None:
            # Only the benchmark exports files for the native comparison.
            pure_packages = base / 'native-pure-packages'
            with ZipFile(BytesIO(snapshot(isolates[1]._environment / 'packages'))) as archive:
                archive.extractall(pure_packages)
            pure_env = dict(native_env, PYTHONPATH=str(pure_packages))
            for count, project in projects.items():
                for tool, arguments in LINTER_ARGUMENTS.items():
                    native_arguments: List[str] = [arg.replace('/dev/null', os.devnull) for arg in arguments]
                    variants = [('native', native_env)]
                    if tool.startswith('mypy'):
                        variants.append(('native_pure', pure_env))
                    for variant, env in variants:
                        name = f'{tool}.{count}_files.{variant}'
                        scenarios[name] = Scenario(partial(checked_native, python, native_arguments, project, env),
                                                   name=name, doc=f'Run native {tool} on {count} files ({variant}).', number=number)
                name = f'ruff.{count}_files.native_only'
                scenarios[name] = Scenario(partial(checked_native, python, ('-m', 'ruff', 'check', '--no-cache', '.'), project, native_env),
                                           name=name, doc=f'Run native Ruff on {count} files; no WASI port is available.', number=number)

        yield BenchmarkSuite(projects, isolates, scenarios, native_env, pure_env, ScenarioGroup(*scenarios.values()))
