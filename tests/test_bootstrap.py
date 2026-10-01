import hashlib
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Event
from typing import Dict
from zipfile import ZipFile

import pytest

from throng_wasm import WasmManager, bootstrap


def archive_bytes(files: Dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def fresh_bundle_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, '_bundle', None)


@pytest.fixture
def bundle_assets(monkeypatch: pytest.MonkeyPatch) -> bytes:
    runtime = archive_bytes({
        'python.wasm': b'(module (func (export "_start")))',
        'lib/python3.13/encodings/__init__.py': b'',
    })
    monkeypatch.setattr(bootstrap, 'BUNDLE_SHA256', hashlib.sha256(runtime).hexdigest())
    return runtime


def test_resource_and_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    data = b'pinned bytes'
    def get_data(package: str, resource: str) -> bytes:
        assert package == 'throng_wasm'
        assert resource == bootstrap.BUNDLE_RESOURCE
        return data

    monkeypatch.setattr(bootstrap, 'get_data', get_data)
    monkeypatch.setattr(bootstrap, 'BUNDLE_SHA256', hashlib.sha256(data).hexdigest())
    assert bootstrap._read_bundle() == data
    monkeypatch.setattr(bootstrap, 'BUNDLE_SHA256', 'incorrect')
    with pytest.raises(ValueError, match='SHA-256'):
        bootstrap._read_bundle()
    monkeypatch.setattr(bootstrap, 'get_data', lambda *_args: None)
    with pytest.raises(FileNotFoundError, match='Missing package resource'):
        bootstrap._read_bundle()


def test_load_once_per_process(monkeypatch: pytest.MonkeyPatch, bundle_assets: bytes) -> None:
    reads = []

    def get_data(package: str, resource: str) -> bytes:
        reads.append((package, resource))
        return bundle_assets

    monkeypatch.setattr(bootstrap, 'get_data', get_data)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: bootstrap.ensure_bundle(), range(2)))
    assert results[0] is results[1]
    assert reads == [('throng_wasm', bootstrap.BUNDLE_RESOURCE)]
    assert (results[0] / 'python.wasm').read_bytes().startswith(b'(module')
    assert bootstrap.ensure_bundle() is results[0]


def test_failed_resource_read_can_retry(monkeypatch: pytest.MonkeyPatch, bundle_assets: bytes) -> None:
    def unavailable(*_args):
        raise FileNotFoundError('missing resource')

    monkeypatch.setattr(bootstrap, 'get_data', unavailable)
    with pytest.raises(RuntimeError, match=r'missing resource.*Reinstall'):
        bootstrap.ensure_bundle()
    assert bootstrap._bundle is None
    monkeypatch.setattr(bootstrap, 'get_data', lambda *_args: bundle_assets)
    assert (bootstrap.ensure_bundle() / 'python.wasm').is_file()


@pytest.mark.parametrize('data', [b'not a zip', archive_bytes({'../escape': b'bad'}), archive_bytes({'incomplete': b'bad'})],
                         ids=['not-a-zip', 'unsafe-path', 'missing-runtime'])
def test_reject_bad_archive(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    monkeypatch.setattr(bootstrap, '_read_bundle', lambda: data)
    with pytest.raises(RuntimeError, match='Could not load'):
        bootstrap.ensure_bundle()
    assert bootstrap._bundle is None


def test_no_environment_or_disk_needed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bundle_assets: bytes) -> None:
    monkeypatch.delenv('THRONG_WASM_HOME', raising=False)
    monkeypatch.delenv('THRONG_WASM_PACKAGES', raising=False)
    monkeypatch.setenv('THRONG_WASM_CACHE', str(tmp_path / 'ignored-cache'))
    monkeypatch.setattr(bootstrap, 'get_data', lambda *_args: bundle_assets)
    manager = WasmManager(tmp_path)
    with manager.scope as isolate:
        assert bootstrap._bundle is None
        assert isolate.run('ls').success
        assert isolate.run('python -c pass').success
    assert bootstrap._bundle is not None
    assert not list(tmp_path.iterdir())


def test_concurrent_retry_does_not_publish_failed_preparation(monkeypatch: pytest.MonkeyPatch, bundle_assets: bytes) -> None:
    entered, release = Event(), Event()
    attempts = []

    def read():
        attempts.append(True)
        if len(attempts) == 1:
            entered.set()
            assert release.wait(5)
            raise OSError('first reader failed')
        return bundle_assets

    monkeypatch.setattr(bootstrap, '_read_bundle', read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(bootstrap.ensure_bundle)
        try:
            assert entered.wait(3)
            second = executor.submit(bootstrap.ensure_bundle)
            assert not second.done()
            assert bootstrap._bundle is None
        finally:
            release.set()
        with pytest.raises(RuntimeError, match='first reader failed'):
            first.result(timeout=3)
        loaded = second.result(timeout=3)
    assert (loaded / 'python.wasm').is_file()
    assert bootstrap.ensure_bundle() is loaded
    assert len(attempts) == 2
