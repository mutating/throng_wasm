import os
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

import pytest

from throng_wasm.memory import MemoryPath
from throng_wasm.state import restore, snapshot


def test_roundtrip(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'empty').mkdir()
    (source / 'nested').mkdir()
    (source / 'nested' / 'данные.bin').write_bytes(b'\x00\xff')
    (source / 'ignored').mkdir()
    (source / 'ignored' / 'private').write_text('hidden')
    (source / 'skip').write_text('hidden')
    target = MemoryPath()
    restore(snapshot(source, ('ignored', 'skip')), target)
    assert (target / 'empty').is_dir()
    assert (target / 'nested' / 'данные.bin').read_bytes() == b'\x00\xff'
    assert not (target / 'ignored').exists()
    assert not (target / 'skip').exists()


def test_links_and_special_files(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    target = tmp_path / 'target'
    target.mkdir()
    try:
        (source / 'link').symlink_to(target, target_is_directory=True)
        (source / 'file-link').symlink_to(tmp_path / 'missing')
    except OSError:
        pytest.skip('Symlinks require privileges on this system.')
    if hasattr(os, 'mkfifo'):
        os.mkfifo(source / 'pipe')
    with ZipFile(BytesIO(snapshot(source))) as archive:
        assert archive.namelist() == []


def test_missing_source(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        snapshot(tmp_path / 'missing')


@pytest.mark.parametrize('name', ['/absolute', '../escape', 'a/../../escape', 'a\\escape', 'C:escape', '.', 'a/.. /escape', 'a./file', 'CON', 'com1.txt', 'COM¹', 'a/NUL'])
def test_unsafe_paths_leave_destination_unchanged(name: str) -> None:
    target = MemoryPath()
    (target / 'existing').write_bytes(b'keep')
    before = snapshot(target)
    data = BytesIO()
    with ZipFile(data, 'w') as archive:
        archive.writestr('valid', 'also must not be written')
        archive.writestr(name, 'bad')
    with pytest.raises(ValueError, match="snapshot"):
        restore(data.getvalue(), target)
    assert snapshot(target) == before


@pytest.mark.parametrize('mode', [0o120777, 0o010644, 0o020644])
def test_reject_special_members(mode: int) -> None:
    data = BytesIO()
    with ZipFile(data, 'w') as archive:
        info = ZipInfo('special')
        info.external_attr = mode << 16
        archive.writestr(info, '../outside')
    with pytest.raises(ValueError, match='Unsupported'):
        restore(data.getvalue())


def test_memory_exclusions_and_duplicate_members() -> None:
    root = MemoryPath()
    (root / 'ignored').mkdir()
    (root / 'ignored/file').write_bytes(b'ignored')
    (root / 'kept').write_bytes(b'kept')
    with ZipFile(BytesIO(snapshot(root, ('ignored',)))) as archive:
        assert archive.namelist() == ['kept']
    data = BytesIO()
    with ZipFile(data, 'w') as archive:
        archive.writestr('same', b'first')
        with pytest.warns(UserWarning, match='Duplicate name'):
            archive.writestr('same', b'second')
    target = MemoryPath()
    (target / 'original').write_bytes(b'untouched')
    with pytest.raises(ValueError, match='Duplicate snapshot'):
        restore(data.getvalue(), target)
    assert [p.name for p in target.iterdir()] == ['original']


@pytest.mark.parametrize('size', [0, 65535, 65536, 65537, 131073])
def test_binary_chunk_boundaries_and_independent_restores(size: int) -> None:
    source = MemoryPath()
    data = (bytes(range(256)) * (size // 256 + 1))[:size]
    (source / 'data').write_bytes(data)
    state = snapshot(source)
    first, second = restore(state), restore(state)
    assert (first / 'data').read_bytes() == data
    (first / 'data').write_bytes(b'changed')
    first.root.closed = True
    assert (second / 'data').read_bytes() == data
    assert (source / 'data').read_bytes() == data


def test_empty_and_compressed_archives() -> None:
    empty = snapshot(MemoryPath())
    assert list(restore(empty).walk()) == []
    buffer = BytesIO()
    with ZipFile(buffer, 'w', compression=ZIP_DEFLATED) as archive:
        archive.writestr('dir/data', b'abc\0' * 40000)
        archive.writestr('dir/', b'')
    restored = restore(buffer.getvalue())
    assert (restored / 'dir/data').read_bytes() == b'abc\0' * 40000


@pytest.mark.parametrize('damage', ['crc', 'truncated', 'file-parent', 'directory-file', 'normalized-duplicate'])
def test_failed_restore_never_publishes_partial_tree(damage: str) -> None:
    target = MemoryPath()
    (target / 'existing').write_bytes(b'keep')
    before = snapshot(target)
    buffer = BytesIO()
    with ZipFile(buffer, 'w') as archive:
        archive.writestr('valid', b'new data that must not be published')
        if damage == 'file-parent':
            archive.writestr('a', b'file')
            archive.writestr('a/child', b'child')
        elif damage == 'directory-file':
            archive.writestr('a/', b'')
            archive.writestr('a', b'file')
        elif damage == 'normalized-duplicate':
            archive.writestr('a/b', b'first')
            archive.writestr('a/./b', b'second')
        else:
            archive.writestr('broken', b'unique payload to corrupt')
    data = buffer.getvalue()
    if damage == 'crc':
        data = data.replace(b'unique payload to corrupt', b'UNIQUE payload to corrupt')
    elif damage == 'truncated':
        data = data[:-30]
    with pytest.raises((BadZipFile, OSError, ValueError)):
        restore(data, target)
    assert snapshot(target) == before


@pytest.mark.parametrize('storage', ['host', 'memory'])
@pytest.mark.parametrize('exclude', [(), ('*',)])
def test_extra_trees_have_separate_prefixes_and_bypass_project_filters(tmp_path: Path, storage: str, exclude: tuple) -> None:
    project = tmp_path if storage == 'host' else MemoryPath()
    (project / 'data').write_bytes(b'project')
    extra = MemoryPath()
    for name in ['packages', 'scripts']:
        (extra / name / 'empty').mkdir(parents=True)
        (extra / name / 'data').write_bytes(name.encode())
    state = snapshot(project, exclude, extra=[extra / 'missing', extra / 'packages', extra / 'scripts'])
    with ZipFile(BytesIO(state)) as archive:
        expected = {'packages/empty/', 'packages/data', 'scripts/empty/', 'scripts/data'}
        if not exclude:
            expected.add('data')
        assert set(archive.namelist()) == expected
        assert all(not name.startswith('/') for name in archive.namelist())
    restored = restore(state)
    assert (restored / 'packages/data').read_bytes() == b'packages'
    assert (restored / 'scripts/data').read_bytes() == b'scripts'
    assert (project / 'data').read_bytes() == b'project'
