"""A private tree of regular files; no host filesystem operations."""

import errno
from dataclasses import dataclass, field
from itertools import count
from pathlib import PurePosixPath
from time import time_ns
from typing import Dict, Iterator, Optional, Tuple, Union

_inodes = count(1)


@dataclass(eq=False)
class Node:
    directory: bool = False
    data: bytes = b''
    null: bool = False
    closed: bool = False
    children: Dict[str, 'Node'] = field(default_factory=dict)
    inode: int = field(default_factory=lambda: next(_inodes))
    mtime: int = field(default_factory=time_ns)
    atime: int = field(default_factory=time_ns)


class MemoryPath:
    """Small path interface for host-side management of an in-memory tree.

    Deliberately not os.PathLike: passing it to open() must never access the host.
    Guest paths are resolved relative to an open directory, without symlinks.
    """

    def __init__(self, root: Optional[Node] = None, parts: Tuple[str, ...] = ()) -> None:
        self.root = Node(directory=True) if root is None else root
        self.parts = parts

    def __truediv__(self, name: Union[str, PurePosixPath]) -> 'MemoryPath':
        path = PurePosixPath(name)
        if path.is_absolute() or '\x00' in str(name):
            raise PermissionError(errno.EACCES, 'Absolute paths and NUL bytes are forbidden')
        parts = list(self.parts)
        for part in path.parts:
            if part == '..':
                if not parts:
                    raise PermissionError(errno.EACCES, 'Path escapes its directory')
                parts.pop()
            else:
                parts.append(part)
        return MemoryPath(self.root, tuple(parts))

    def __str__(self) -> str:
        return '/'.join(self.parts) or '.'

    @property
    def name(self) -> str:
        return self.parts[-1] if self.parts else ''

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.name).suffix

    @property
    def parent(self) -> 'MemoryPath':
        return MemoryPath(self.root, self.parts[:-1])

    @property
    def node(self) -> Node:
        if self.root.closed:
            raise FileNotFoundError(errno.ENOENT, 'The memory tree has been released')
        current = self.root
        for part in self.parts:
            if not current.directory:
                raise NotADirectoryError(errno.ENOTDIR, 'Not a directory', str(self))
            try:
                current = current.children[part]
            except KeyError:
                raise FileNotFoundError(errno.ENOENT, 'No such file or directory', str(self)) from None
        return current

    def exists(self) -> bool:
        try:
            _ = self.node
        except (FileNotFoundError, NotADirectoryError):
            return False
        return True

    def is_dir(self) -> bool:
        return self.exists() and self.node.directory

    def is_file(self) -> bool:
        return self.exists() and not self.node.directory

    def mkdir(self, parents: bool = False, exist_ok: bool = False) -> None:
        if self.exists():
            if exist_ok and self.is_dir():
                return
            raise FileExistsError(errno.EEXIST, 'File exists', str(self))
        if parents:
            self.parent.mkdir(parents=True, exist_ok=True)
        parent = self.parent.node
        if not parent.directory:
            raise NotADirectoryError(errno.ENOTDIR, 'Not a directory', str(self.parent))
        parent.children[self.name] = Node(directory=True)

    def read_bytes(self) -> bytes:
        node = self.node
        if node.directory:
            raise IsADirectoryError(errno.EISDIR, 'Is a directory', str(self))
        return node.data

    def read_text(self, encoding: str = 'utf-8') -> str:
        return self.read_bytes().decode(encoding)

    def write_bytes(self, data: bytes) -> int:
        parent = self.parent.node
        if not self.parts or self.is_dir():
            raise IsADirectoryError(errno.EISDIR, 'Is a directory', str(self))
        if not parent.directory:
            raise NotADirectoryError(errno.ENOTDIR, 'Not a directory', str(self.parent))
        node = parent.children.setdefault(self.name, Node())
        node.data = bytes(data)
        node.mtime = time_ns()
        return len(data)

    def write_text(self, data: str, encoding: str = 'utf-8') -> int:
        self.write_bytes(data.encode(encoding))
        return len(data)

    def iterdir(self) -> Iterator['MemoryPath']:
        node = self.node
        if not node.directory:
            raise NotADirectoryError(errno.ENOTDIR, 'Not a directory', str(self))
        return iter([self / name for name in sorted(node.children)])

    def walk(self) -> Iterator['MemoryPath']:
        for child in self.iterdir():
            yield child
            if child.is_dir():
                yield from child.walk()

    def relative_to(self, base: 'MemoryPath') -> PurePosixPath:
        if self.root is not base.root or self.parts[:len(base.parts)] != base.parts:
            raise ValueError('Paths do not share the same base')
        return PurePosixPath(*self.parts[len(base.parts):])

    def glob(self, pattern: str) -> Iterator['MemoryPath']:
        return (path for path in self.walk() if len(path.relative_to(self).parts) == len(PurePosixPath(pattern).parts)
                and path.relative_to(self).match(pattern))

    def unlink(self) -> None:
        if self.node.directory:
            raise IsADirectoryError(errno.EISDIR, 'Is a directory', str(self))
        del self.parent.node.children[self.name]

    def rmdir(self) -> None:
        node = self.node
        if not node.directory:
            raise NotADirectoryError(errno.ENOTDIR, 'Not a directory', str(self))
        if node.children or not self.parts:
            raise OSError(errno.ENOTEMPTY, 'Directory not empty', str(self))
        del self.parent.node.children[self.name]

    def replace(self, target: 'MemoryPath') -> 'MemoryPath':
        node, parent = self.node, target.parent.node
        if not self.parts or not target.parts or not parent.directory:
            raise OSError(errno.EINVAL, 'Invalid rename')
        if self.root is target.root and target.parts[:len(self.parts)] == self.parts:
            if self.parts == target.parts:
                return target
            raise OSError(errno.EINVAL, 'Cannot move a directory into itself')
        if target.exists():
            old = target.node
            if old is node:
                return target
            if old.directory != node.directory:
                raise OSError(errno.EISDIR if old.directory else errno.ENOTDIR, 'Incompatible rename target')
            if old.children:
                raise OSError(errno.ENOTEMPTY, 'Directory not empty')
        parent.children[target.name] = node
        del self.parent.node.children[self.name]
        return target

    def publish(self, name: str, prepared: 'MemoryPath') -> None:
        """Replace one whole subtree with a single reference assignment."""
        self.node.children[name] = prepared.node
