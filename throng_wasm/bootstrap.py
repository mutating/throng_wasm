"""Read the packaged CPython runtime into memory, without network or extraction on disk."""

import hashlib
from pkgutil import get_data
from threading import Lock
from typing import Optional
from zipfile import BadZipFile

from throng_wasm.memory import MemoryPath
from throng_wasm.state import restore

BUNDLE_RESOURCE = 'data/python-3.13.11-wasi_sdk-24.zip'
BUNDLE_SHA256 = 'e99a617738ade87cd263aa46cace7173faa91b5de994499c83e49d132c40bb77'
_bundle: Optional[MemoryPath] = None
_lock = Lock()


def _read_bundle() -> bytes:
    data = get_data('throng_wasm', BUNDLE_RESOURCE)
    if data is None:
        raise FileNotFoundError(f'Missing package resource: {BUNDLE_RESOURCE}')
    if hashlib.sha256(data).hexdigest() != BUNDLE_SHA256:
        raise ValueError(f'SHA-256 mismatch for bundled {BUNDLE_RESOURCE}')
    return data


def ensure_bundle() -> MemoryPath:
    global _bundle  # noqa: PLW0603
    with _lock:
        if _bundle is None:
            try:
                runtime = restore(_read_bundle())
                if not all((runtime / name).is_file() for name in ('python.wasm', 'lib/python3.13/encodings/__init__.py')):
                    raise ValueError('The packaged bundle is missing required runtime files.')
                _bundle = runtime
            except (OSError, ValueError, BadZipFile) as exception:
                raise RuntimeError(f'Could not load the packaged WASI runtime: {exception}. Reinstall throng-wasm or provide THRONG_WASM_HOME.') from exception
        return _bundle
