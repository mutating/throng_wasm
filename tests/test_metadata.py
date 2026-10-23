"""Resource metadata and identity follow the throng 0.0.10 isolate contract."""

from pathlib import Path

import pytest
from throng.abstracts.meta import Benchmarks, IsolateMeta, Resources

from throng_wasm import WasmIsolate, WasmManager, WasmRuntime
from throng_wasm.state import snapshot


def test_metadata_without_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WasmRuntime, 'from_environment', lambda: pytest.fail('Metadata initialized the runtime'))
    isolate = WasmIsolate(snapshot(tmp_path))
    try:
        identifier = isolate.get_id()
        assert isinstance(identifier, str)
        assert identifier
        meta = isolate.meta
        assert meta == IsolateMeta(identifier, Resources(Benchmarks(None, None), shared=True, shared_with=None))
        assert isolate.get_benchmarks() == meta.resources.benchmarks
        description = isolate.get_resources_description()
        assert description.shared is True
        assert description.shared_with is None
        assert isolate.meta == meta
        assert isolate.runtime is None
    finally:
        isolate.kill()
    assert isolate.meta == meta


def test_identity_survives_commands_but_not_snapshot_restore(tmp_path: Path, runtime: WasmRuntime) -> None:
    manager = WasmManager(tmp_path, runtime=runtime)
    with manager.scope as isolate:
        meta = isolate.meta
        assert isolate.run('python').success
        state = isolate.read()
        assert isolate.get_id() == meta.id
        assert isolate.meta == meta
    assert isolate.meta == meta

    restored = manager.get(state)
    try:
        direct = WasmIsolate(state, runtime=runtime)
        try:
            assert len({meta.id, restored.get_id(), direct.get_id()}) == 3
            assert restored.meta.id == restored.get_id()
            assert direct.meta.id == direct.get_id()
            assert restored.meta.resources == direct.meta.resources == meta.resources
        finally:
            direct.kill()
    finally:
        restored.kill()
