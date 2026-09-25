"""Portable snapshots containing only regular files and directories."""

from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Sequence, Union
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from dirstree import Crawler
from pathspec import PathSpec

from throng_wasmtime.memory import MemoryPath

WINDOWS_DEVICES = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {
    f'{prefix}{suffix}' for prefix in ('COM', 'LPT') for suffix in '123456789¹²³'
}

DEFAULT_EXCLUDE = ('.git', '.venv', 'venv', '__pycache__', '.mypy_cache', '.pytest_cache', '.ruff_cache')


def _write_host_tree(archive: ZipFile, path: Path, spec: PathSpec, check: Callable[[], None]) -> None:
    def include_host_item(item: Path) -> bool:
        check()
        # Crawler matches its exclude patterns against host paths. Normalize
        # here so anchored patterns also work with absolute project paths,
        # without changing the process-wide working directory.
        name = item.relative_to(path).as_posix()
        return (not item.is_symlink() and (item.is_file() or item.is_dir())
                and not spec.match_file(name + ('/' if item.is_dir() else '')))

    for item in Crawler(path, only_files=False, filter=include_host_item, raise_on_cancel=True):
        check()
        archive.write(item, item.relative_to(path).as_posix())


def _write_memory_file(archive: ZipFile, path: MemoryPath, name: str, check: Callable[[], None]) -> None:
    data = memoryview(path.node.data)
    with archive.open(ZipInfo(name), 'w', force_zip64=len(data) >= 2 ** 31) as output:
        for offset in range(0, len(data), 65536):
            check()
            output.write(data[offset:offset + 65536])


def _write_memory_tree(archive: ZipFile, path: MemoryPath, check: Callable[[], None], *,
                       prefix: str, spec: Optional[PathSpec]) -> None:
    if not path.exists():
        return
    for item in path.walk():
        check()
        name = str(item.relative_to(path))
        directory = item.is_dir()
        if spec is not None and spec.match_file(name + ('/' if directory else '')):
            continue
        name = prefix + name
        if directory:
            archive.writestr(ZipInfo(name + '/'), b'')
        else:
            _write_memory_file(archive, item, name, check)


def snapshot(path: Union[Path, MemoryPath], exclude: Sequence[str] = (), *, extra: Sequence[MemoryPath] = (),
             check: Callable[[], None] = lambda: None) -> bytes:
    check()
    if not path.is_dir():
        raise NotADirectoryError(str(path))
    spec = PathSpec.from_lines('gitwildmatch', exclude)
    data = BytesIO()
    with ZipFile(data, 'w', compression=ZIP_STORED) as archive:
        for source in (path, *extra):
            check()
            if isinstance(source, MemoryPath):
                # Installed environments keep their namespace and bypass project exclusions.
                _write_memory_tree(archive, source, check,
                                   prefix='' if source is path else f'{source.name}/',
                                   spec=spec if source is path else None)
            else:
                _write_host_tree(archive, source, spec, check)
    check()
    return data.getvalue()


def restore(state: bytes, path: Optional[MemoryPath] = None, *, check: Callable[[], None] = lambda: None) -> MemoryPath:
    check()
    prepared = MemoryPath()
    with ZipFile(BytesIO(state)) as archive:
        # Validate the entire snapshot before writing anything; never extract links.
        for member in archive.infolist():
            check()
            name = PurePosixPath(member.filename)
            if name.is_absolute() or '..' in name.parts or '\\' in member.filename or ':' in member.filename:
                raise ValueError(f'Unsafe snapshot path: {member.filename!r}')
            if any(part.endswith((' ', '.')) or part.split('.')[0].upper() in WINDOWS_DEVICES for part in name.parts):
                raise ValueError(f'Non-portable snapshot path: {member.filename!r}')
            if not name.parts or (member.external_attr >> 16) & 0o170000 not in {0, 0o100000, 0o040000}:
                raise ValueError(f'Unsupported snapshot member: {member.filename!r}')
        for member in archive.infolist():
            check()
            target = prepared / member.filename
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise ValueError(f'Duplicate snapshot member: {member.filename!r}')
                with archive.open(member) as stream:
                    data = bytearray()
                    while True:
                        check()
                        chunk = stream.read(65536)
                        if not chunk:
                            break
                        data.extend(chunk)
                target.write_bytes(bytes(data))
    check()
    if path is None:
        return prepared
    path.node.children.update(prepared.node.children)
    return path
