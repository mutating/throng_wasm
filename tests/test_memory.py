import errno
from pathlib import PurePosixPath

import pytest

from throng_wasmtime.memory import MemoryPath, Node
from throng_wasmtime.state import restore, snapshot


def test_tree_and_snapshot() -> None:
    root = MemoryPath()
    nested = root / 'a/b'
    nested.mkdir(parents=True)
    file = nested / 'данные.bin'
    file.write_bytes(b'\x00\xff')
    assert file.read_bytes() == b'\x00\xff'
    assert file.name == 'данные.bin'
    assert file.suffix == '.bin'
    assert file.relative_to(root) == PurePosixPath('a/b/данные.bin')
    assert [str(p) for p in root.glob('a/*')] == ['a/b']
    assert [str(p) for p in root.walk()] == ['a', 'a/b', 'a/b/данные.bin']
    assert str(root / 'a/b/../c') == 'a/c'
    assert str(root) == '.'
    assert root.name == ''
    assert not file.is_dir()
    assert not (root / 'missing').is_file()
    clone = restore(snapshot(root))
    assert (clone / 'a/b/данные.bin').read_bytes() == file.read_bytes()
    assert snapshot(clone) == snapshot(root)
    assert (root / 'text').write_text('привет') == 6
    assert (root / 'text').read_text() == 'привет'
    assert str((root / 'text').replace(root / 'text')) == 'text'
    (root / 'text').replace(root / 'renamed')
    assert not (root / 'text').exists()
    (root / 'renamed').unlink()
    file.unlink()
    nested.rmdir()
    (root / 'a').rmdir()
    assert not list(root.iterdir())


@pytest.mark.parametrize('name', ['/absolute', '../outside', 'a/../../outside', 'nul\x00'])
def test_no_escape(name: str) -> None:
    with pytest.raises(PermissionError):
        MemoryPath() / name


def test_path_errors_and_atomic_publish() -> None:
    root = MemoryPath()
    (root / 'file').write_bytes(b'old')
    (root / 'directory').mkdir()
    for operation, exception in [
        (lambda: (root / 'file/nested').mkdir(), NotADirectoryError),
        (lambda: (root / 'file/nested').write_bytes(b''), NotADirectoryError),
        (lambda: (root / 'file').iterdir(), NotADirectoryError),
        (lambda: (root / 'file').rmdir(), NotADirectoryError),
        (lambda: root.read_bytes(), IsADirectoryError),
        (lambda: root.write_bytes(b''), IsADirectoryError),
        (lambda: (root / 'directory').write_bytes(b''), IsADirectoryError),
        (lambda: root.unlink(), IsADirectoryError),
        (lambda: root.mkdir(), FileExistsError),
        (lambda: (root / 'file').mkdir(exist_ok=True), FileExistsError),
        (lambda: (root / 'missing').read_bytes(), FileNotFoundError),
        (lambda: (root / 'absent/child').mkdir(), FileNotFoundError),
    ]:
        with pytest.raises(exception):
            operation()
    assert not (root / 'file/nested').exists()
    root.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match='same base'):
        (root / 'file').relative_to(MemoryPath())
    with pytest.raises(ValueError, match='same base'):
        (root / 'file').relative_to(root / 'directory')
    with pytest.raises(OSError, match=r"directory|rename|Directory") as error:
        root.rmdir()
    assert error.value.errno == errno.ENOTEMPTY
    for source, target, code in [(root, root / 'x', errno.EINVAL),
                                  (root / 'file', root, errno.EINVAL),
                                  (root / 'file', root / 'file/x', errno.EINVAL),
                                  (root / 'directory', root / 'directory/x', errno.EINVAL),
                                  (root / 'file', root / 'directory', errno.EISDIR),
                                  (root / 'directory', root / 'file', errno.ENOTDIR)]:
        with pytest.raises(OSError, match=r"directory|rename|Directory") as error:
            source.replace(target)
        assert error.value.errno == code
    (root / 'other').mkdir()
    (root / 'other/child').write_bytes(b'')
    with pytest.raises(OSError, match=r"directory|rename|Directory") as error:
        (root / 'directory').replace(root / 'other')
    assert error.value.errno == errno.ENOTEMPTY
    prepared = MemoryPath()
    (prepared / 'new').write_bytes(b'new')
    root.publish('directory', prepared)
    assert (root / 'directory/new').read_bytes() == b'new'
    assert (root / 'file').read_bytes() == b'old'
    root.root.children.clear()
    root.root.closed = True
    assert not root.exists()


def test_replace_existing_file_and_empty_directory() -> None:
    root = MemoryPath()
    (root / 'one').write_bytes(b'one')
    (root / 'two').write_bytes(b'two')
    (root / 'one').replace(root / 'two')
    assert (root / 'two').read_bytes() == b'one'
    (root / 'a').mkdir()
    (root / 'b').mkdir()
    (root / 'a').replace(root / 'b')
    assert not (root / 'a').exists()
    assert (root / 'b').is_dir()
    file_root = MemoryPath(Node()) / 'x'
    with pytest.raises(NotADirectoryError):
        _ = file_root.node


def test_rename_and_unlink_preserve_open_node_identity() -> None:
    source, destination = MemoryPath(), MemoryPath()
    (source / 'tree').mkdir()
    (source / 'tree/file').write_bytes(b'first')
    opened = (source / 'tree/file').node
    (source / 'tree').replace(destination / 'moved')
    assert not (source / 'tree').exists()
    assert (destination / 'moved/file').node is opened
    (destination / 'replacement').write_bytes(b'second')
    new_node = (destination / 'replacement').node
    (destination / 'replacement').replace(destination / 'moved/file')
    assert (destination / 'moved/file').node is new_node
    assert opened.data == b'first'
    (destination / 'moved/file').unlink()
    assert new_node.data == b'second'
    assert not list((destination / 'moved').iterdir())
