from pathlib import Path

import pytest

from throng_wasmtime import WasmRuntime


@pytest.fixture
def wasm_home(tmp_path: Path) -> Path:
    home = tmp_path / 'runtime'
    (home / 'lib').mkdir(parents=True)
    return home


@pytest.fixture
def runtime(wasm_home: Path) -> WasmRuntime:
    (wasm_home / 'python.wasm').write_text('(module (func (export "_start")))')
    return WasmRuntime(wasm_home)
