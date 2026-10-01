"""Validate benchmark inputs and failure handling, not machine-dependent timing."""

import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.test_installer import Index
from throng_wasm import WasmIsolate, WasmResult, benchmarks
from throng_wasm.benchmarks import checked_native, checked_wasm


@pytest.fixture
def index(monkeypatch: pytest.MonkeyPatch) -> Index:
    packages = Index()
    monkeypatch.setattr('throng_wasm.installer.download', packages.download)
    return packages


@pytest.mark.parametrize('success', [True, False])
def test_checked_wasm_does_not_measure_failed_commands(success: bool) -> None:
    isolate = Mock(spec=WasmIsolate)
    isolate.run.return_value = WasmResult(success, stdout='output', stderr='error')
    if success:
        checked_wasm(isolate, 'python -m tool')
    else:
        with pytest.raises(RuntimeError, match=r'python -m tool: output\nerror'):
            checked_wasm(isolate, 'python -m tool')
    isolate.run.assert_called_once_with('python -m tool')


@pytest.mark.parametrize('code', [0, 2])
def test_checked_native_preserves_argv_environment_and_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], code, 'output', 'error'))
    monkeypatch.setattr(subprocess, 'run', run)
    env = {'KEY': 'value'}
    args = ['-m', 'tool', 'a file.py']
    if code:
        with pytest.raises(RuntimeError, match=r'output\nerror'):
            checked_native('python path', args, tmp_path, env)
    else:
        checked_native('python path', args, tmp_path, env)
    run.assert_called_once_with(['python path', *args], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)


@pytest.mark.parametrize(('number', 'cold_number'), [(0, 1), (-1, 1), (1, 0), (1, -1)])
def test_invalid_iterations_fail_before_preparing_resources(monkeypatch: pytest.MonkeyPatch, number: int, cold_number: int) -> None:
    monkeypatch.setattr(benchmarks.WasmRuntime, 'from_environment', lambda: pytest.fail('Runtime preparation started'))
    with pytest.raises(ValueError, match='positive'), benchmarks.prepare(number=number, cold_number=cold_number):
        pytest.fail('Invalid iteration count was accepted')


def test_preparation_exports_identical_corpus_and_packages_for_native_comparison(index: Index, monkeypatch: pytest.MonkeyPatch) -> None:
    index.add('example')
    monkeypatch.setattr(benchmarks, 'PACKAGES', ('example',))
    monkeypatch.setenv('PYTHONPATH', 'must-not-leak')
    monkeypatch.setenv('PYTHONHOME', 'must-not-leak')
    monkeypatch.setenv('COVERAGE_PROCESS_START', 'must-not-leak')
    with benchmarks.prepare(native_python=sys.executable, number=1, cold_number=1) as suite:
        projects = list(suite.projects.values())
        isolates = list(suite.isolates.values())
        assert not {'PYTHONPATH', 'PYTHONHOME', 'COVERAGE_PROCESS_START'} & suite.native_env.keys()
        for count, project in suite.projects.items():
            files = sorted(project.glob('*.py'))
            assert len(files) == count
            for file in files:
                assert (suite.isolates[count].path / file.name).read_bytes() == file.read_bytes()
            code = (f'import example; from pathlib import Path; assert example.VERSION == "1.0"; '
                    f'assert len(list(Path(".").glob("module_*.py"))) == {count}')
            checked_native(sys.executable, ['-c', code], project, suite.pure_env)
            checked_wasm(suite.isolates[count], 'python -c ' + shlex.quote(code))
    assert all(not project.exists() for project in projects)
    assert all(not isolate.path.exists() for isolate in isolates)
    assert not Path(suite.pure_env['PYTHONPATH']).exists()


@pytest.mark.parametrize('failure', ['install', 'body'])
def test_preparation_cleans_up_on_failure(index: Index, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    index.add('example')
    monkeypatch.setattr(benchmarks, 'PACKAGES', ('example',))
    isolates = []
    projects = []
    install = WasmIsolate.install
    manager = benchmarks.WasmManager

    def record_project(path, **kwargs):
        projects.append(path)
        return manager(path, **kwargs)

    def install_or_fail(isolate, *packages):
        isolates.append(isolate)
        if failure == 'install' and len(isolates) == 2:
            raise RuntimeError('preparation failed')
        install(isolate, *packages)

    monkeypatch.setattr(benchmarks, 'WasmManager', record_project)
    monkeypatch.setattr(WasmIsolate, 'install', install_or_fail)
    with pytest.raises(RuntimeError, match='preparation failed'), benchmarks.prepare(number=1, cold_number=1):
        raise RuntimeError('preparation failed')
    assert all(not project.exists() for project in projects)
    assert all(not isolate.path.exists() for isolate in isolates)
