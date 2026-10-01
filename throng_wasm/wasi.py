"""WASI preview1 callbacks backed by private memory, never host paths.

ABI: https://github.com/WebAssembly/WASI/blob/snapshot-01/phases/snapshot/docs.md
Unsupported links and sockets return NOTSUP. Guest pointers are bounds checked
before access; no guest path is passed to a host filesystem API.
"""

# The callback signatures are fixed by the WASI ABI.
# ruff: noqa: PLR0913

import errno
import os
import struct
import time
from dataclasses import dataclass
from functools import partial
from threading import Event
from typing import Callable, List, Literal, Optional, Protocol, Sequence, Tuple, cast

import wasmtime

from throng_wasm.memory import MemoryPath, Node

# WASI preview1 numeric values. Keep these as ints for callback ABI compatibility.
ERRNO_SUCCESS = 0
ERRNO_ACCES = 2
ERRNO_BADF = 8
ERRNO_EXIST = 20
ERRNO_FAULT = 21
ERRNO_FBIG = 22
ERRNO_INVAL = 28
ERRNO_IO = 29
ERRNO_ISDIR = 31
ERRNO_MFILE = 33
ERRNO_NAMETOOLONG = 37
ERRNO_NOENT = 44
ERRNO_NOMEM = 48
ERRNO_NOSYS = 52
ERRNO_NOTDIR = 54
ERRNO_NOTEMPTY = 55
ERRNO_NOTSUP = 58
ERRNO_PERM = 63
ERRNO_ROFS = 69
ERRNO_SPIPE = 70
ERRNO_XDEV = 75
ERRNO_NOTCAPABLE = 76

READ = 1 << 1
WRITE = 1 << 6
FD_DATASYNC = 1 << 0
FD_SEEK = 1 << 2
FD_FDSTAT_SET_FLAGS = 1 << 3
FD_SYNC = 1 << 4
FD_TELL = 1 << 5
FD_ADVISE = 1 << 7
PATH_CREATE_DIRECTORY = 1 << 9
PATH_CREATE_FILE = 1 << 10
PATH_LINK_TARGET = 1 << 12
PATH_OPEN = 1 << 13
FD_READDIR = 1 << 14
PATH_RENAME_SOURCE = 1 << 16
PATH_RENAME_TARGET = 1 << 17
PATH_FILESTAT_GET = 1 << 18
PATH_FILESTAT_SET_SIZE = 1 << 19
PATH_FILESTAT_SET_TIMES = 1 << 20
FD_FILESTAT_GET = 1 << 21
FD_FILESTAT_SET_SIZE = 1 << 22
FD_FILESTAT_SET_TIMES = 1 << 23
PATH_SYMLINK = 1 << 24
PATH_REMOVE_DIRECTORY = 1 << 25
PATH_UNLINK_FILE = 1 << 26
ALL_RIGHTS = (1 << 30) - 1
WRITE_RIGHTS = (
    WRITE | PATH_CREATE_DIRECTORY | PATH_CREATE_FILE | PATH_LINK_TARGET
    | PATH_RENAME_SOURCE | PATH_RENAME_TARGET | PATH_FILESTAT_SET_SIZE | PATH_FILESTAT_SET_TIMES
    | FD_FILESTAT_SET_SIZE | FD_FILESTAT_SET_TIMES | PATH_SYMLINK | PATH_REMOVE_DIRECTORY | PATH_UNLINK_FILE
)

OFLAG_CREAT = 1
OFLAG_DIRECTORY = 2
OFLAG_EXCL = 4
OFLAG_TRUNC = 8
OFLAGS_ALL = OFLAG_CREAT | OFLAG_DIRECTORY | OFLAG_EXCL | OFLAG_TRUNC

FDFLAG_APPEND = 1
FDFLAG_DSYNC = 2
FDFLAG_NONBLOCK = 4
FDFLAG_RSYNC = 8
FDFLAG_SYNC = 16
FDFLAGS_ALL = FDFLAG_APPEND | FDFLAG_DSYNC | FDFLAG_NONBLOCK | FDFLAG_RSYNC | FDFLAG_SYNC

FSTFLAG_ATIM = 1
FSTFLAG_ATIM_NOW = 2
FSTFLAG_MTIM = 4
FSTFLAG_MTIM_NOW = 8
FSTFLAGS_ATIME = FSTFLAG_ATIM | FSTFLAG_ATIM_NOW
FSTFLAGS_MTIME = FSTFLAG_MTIM | FSTFLAG_MTIM_NOW
FSTFLAGS_ALL = FSTFLAGS_ATIME | FSTFLAGS_MTIME

FILETYPE_CHARACTER_DEVICE = 2
FILETYPE_DIRECTORY = 3
FILETYPE_REGULAR_FILE = 4
EVENTTYPE_CLOCK = 0
EVENTTYPE_FD_READ = 1
EVENTTYPE_FD_WRITE = 2
SUBCLOCK_ABSTIME = 1

MAX_CREATED_NODES = 65536
MAX_IOVECS = 1024
MAX_SUBSCRIPTIONS = 1024
MAX_PATH_BYTES = 4096
MAX_NAME_BYTES = 255
U64_MASK = (1 << 64) - 1

ERRORS = {cast(int, getattr(errno, name)): value for name, value in (
    ('EACCES', ERRNO_ACCES), ('EBADF', ERRNO_BADF), ('EEXIST', ERRNO_EXIST), ('EFAULT', ERRNO_FAULT), ('EFBIG', ERRNO_FBIG),
    ('EINVAL', ERRNO_INVAL), ('EIO', ERRNO_IO), ('EISDIR', ERRNO_ISDIR), ('ENOENT', ERRNO_NOENT), ('ENOMEM', ERRNO_NOMEM),
    ('ENOSYS', ERRNO_NOSYS), ('ENOTDIR', ERRNO_NOTDIR), ('ENOTEMPTY', ERRNO_NOTEMPTY), ('ENOTSUP', ERRNO_NOTSUP), ('EPERM', ERRNO_PERM),
    ('EROFS', ERRNO_ROFS), ('ESPIPE', ERRNO_SPIPE), ('EXDEV', ERRNO_XDEV),
)}


class WasiError(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


class GuestMemory:
    def __init__(self, caller: wasmtime.Caller) -> None:
        self.caller = caller
        self.memory = caller.get('memory')

    def check(self, address: int, size: int) -> None:
        if not isinstance(self.memory, wasmtime.Memory) or address < 0 or size < 0 or address + size > self.memory.data_len(self.caller):
            raise WasiError(ERRNO_FAULT)

    def read(self, address: int, size: int) -> bytes:
        self.check(address, size)
        assert isinstance(self.memory, wasmtime.Memory)
        return bytes(self.memory.read(self.caller, address, address + size))

    def write(self, address: int, data: bytes) -> None:
        self.check(address, len(data))
        if data:
            assert isinstance(self.memory, wasmtime.Memory)
            self.memory.write(self.caller, data, address)

    def unpack(self, fmt: str, address: int) -> Tuple[int, ...]:
        return cast(Tuple[int, ...], struct.unpack('<' + fmt, self.read(address, struct.calcsize('<' + fmt))))

    def pack(self, fmt: str, address: int, *values: int) -> None:
        self.write(address, struct.pack('<' + fmt, *values))


@dataclass
class Descriptor:
    node: Node
    rights: int = ALL_RIGHTS
    inheriting: int = ALL_RIGHTS
    readonly: bool = False
    position: int = 0
    flags: int = 0
    preopen: Optional[bytes] = None


class WasiCallback(Protocol):
    def __call__(self, memory: GuestMemory, *args: int) -> None: ...  # pragma: no cover -- typing-only signature


class MemoryWasi:
    def __init__(self, args: Sequence[str], env: Sequence[Tuple[str, str]],
                 mounts: Sequence[Tuple[str, MemoryPath, bool]], stopped: Event, limit: int, *,
                 check: Callable[[], None] = lambda: None) -> None:
        self.check = check
        self.args = [value.encode() + b'\0' for value in args]
        self.env = [f'{name}={value}'.encode() + b'\0' for name, value in env]
        self.stopped, self.limit = stopped, limit
        self.stdout, self.stderr = Node(), Node()
        self.fds = {0: Descriptor(Node(null=True), rights=READ | FD_FILESTAT_GET),
                    1: Descriptor(self.stdout, rights=WRITE | FD_FILESTAT_GET), 2: Descriptor(self.stderr, rights=WRITE | FD_FILESTAT_GET)}
        self.used = 0
        self.created = 0
        for name, path, readonly in mounts:
            check()
            rights = ALL_RIGHTS & ~WRITE_RIGHTS if readonly else ALL_RIGHTS
            self.fds[len(self.fds)] = Descriptor(path.node, rights=rights, inheriting=rights, readonly=readonly, preopen=name.encode())
            if not readonly:
                for item in path.walk():
                    check()
                    self.used += len(item.node.data)
        self.next_fd = len(self.fds)

    def link(self, linker: wasmtime.Linker, module: wasmtime.Module) -> None:
        for item in module.imports:
            self.check()
            if item.module != 'wasi_snapshot_preview1' or not isinstance(item.type, wasmtime.FuncType):
                raise ValueError(f'Unsupported WASM import: {item.module}.{item.name}')
            assert item.name is not None
            if item.name == 'proc_exit':
                # Native ExitTrap avoids wasmtime-py's process-global storage
                # for exceptions raised by Python callbacks.
                continue
            linker.define_func(item.module, item.name, item.type, partial(self.invoke, item.name), access_caller=True)

    def invoke(self, name: str, caller: wasmtime.Caller, *args: int) -> int:
        try:
            method = cast(Optional[WasiCallback], getattr(self, 'w_' + name, None))
            if method is None:
                return ERRNO_NOTSUP
            method(GuestMemory(caller), *args)
            return ERRNO_SUCCESS
        except WasiError as exception:
            return exception.code
        except OSError as exception:
            return ERRORS.get(exception.errno or 0, ERRNO_IO)
        except MemoryError:
            return ERRNO_NOMEM

    def descriptor(self, fd: int, right: int = 0) -> Descriptor:
        if fd not in self.fds:
            raise WasiError(ERRNO_BADF)
        result = self.fds[fd]
        if right & ~result.rights:
            raise WasiError(ERRNO_NOTCAPABLE)
        return result

    def path(self, memory: GuestMemory, fd: int, pointer: int, length: int, write: bool = False) -> MemoryPath:
        descriptor = self.descriptor(fd)
        if not descriptor.node.directory:
            raise WasiError(ERRNO_NOTDIR)
        if write and descriptor.readonly:
            raise WasiError(ERRNO_ROFS)
        if length > MAX_PATH_BYTES:
            raise WasiError(ERRNO_NAMETOOLONG)
        raw = memory.read(pointer, length)
        if not raw or b'\0' in raw:
            raise WasiError(ERRNO_INVAL)
        if any(len(part) > MAX_NAME_BYTES for part in raw.split(b'/')):
            raise WasiError(ERRNO_NAMETOOLONG)
        try:
            decoded = raw.decode('utf-8', errors='surrogateescape')
            path = MemoryPath(descriptor.node) / decoded
            prefix = MemoryPath(descriptor.node)
            for component in decoded.rstrip('/').split('/')[:-1]:
                prefix = prefix / component
                if not prefix.node.directory:
                    raise WasiError(ERRNO_NOTDIR)
            if raw.endswith(b'/') and path.exists() and not path.is_dir():
                raise WasiError(ERRNO_NOTDIR)
            return path
        except PermissionError:
            raise WasiError(ERRNO_NOTCAPABLE) from None

    def resize(self, node: Node, size: int) -> None:
        if size < 0 or size > self.limit:
            raise WasiError(ERRNO_FBIG)
        growth = size - len(node.data)
        if self.used + growth > self.limit:
            raise WasiError(ERRNO_NOMEM)
        node.data = node.data[:size] + b'\0' * max(0, growth)
        node.mtime = time.time_ns()
        self.used += growth

    @staticmethod
    def filetype(node: Node) -> int:
        return FILETYPE_DIRECTORY if node.directory else FILETYPE_CHARACTER_DEVICE if node.null else FILETYPE_REGULAR_FILE

    def filestat(self, memory: GuestMemory, pointer: int, node: Node) -> None:
        memory.pack('QQB7xQQQQQ', pointer, 0, node.inode, self.filetype(node), 1, len(node.data), node.atime, node.mtime, node.mtime)

    @staticmethod
    def strings_size(memory: GuestMemory, count: int, size: int, values: List[bytes]) -> None:
        memory.pack('I', count, len(values))
        memory.pack('I', size, sum(map(len, values)))

    @staticmethod
    def strings_get(memory: GuestMemory, pointers: int, buffer: int, values: List[bytes]) -> None:
        for index, value in enumerate(values):
            memory.pack('I', pointers + index * 4, buffer)
            memory.write(buffer, value)
            buffer += len(value)

    def w_args_sizes_get(self, memory: GuestMemory, count: int, size: int) -> None:
        self.strings_size(memory, count, size, self.args)

    def w_args_get(self, memory: GuestMemory, pointers: int, buffer: int) -> None:
        self.strings_get(memory, pointers, buffer, self.args)

    def w_environ_sizes_get(self, memory: GuestMemory, count: int, size: int) -> None:
        self.strings_size(memory, count, size, self.env)

    def w_environ_get(self, memory: GuestMemory, pointers: int, buffer: int) -> None:
        self.strings_get(memory, pointers, buffer, self.env)

    def w_fd_prestat_get(self, memory: GuestMemory, fd: int, pointer: int) -> None:
        name = self.descriptor(fd).preopen
        if name is None:
            raise WasiError(ERRNO_BADF)
        memory.pack('II', pointer, 0, len(name))

    def w_fd_prestat_dir_name(self, memory: GuestMemory, fd: int, pointer: int, length: int) -> None:
        name = self.descriptor(fd).preopen
        if name is None:
            raise WasiError(ERRNO_BADF)
        if length < len(name):
            raise WasiError(ERRNO_NAMETOOLONG)
        memory.write(pointer, name)

    def w_fd_fdstat_get(self, memory: GuestMemory, fd: int, pointer: int) -> None:
        d = self.descriptor(fd)
        memory.pack('BxH4xQQ', pointer, self.filetype(d.node), d.flags, d.rights, d.inheriting)

    def w_fd_fdstat_set_flags(self, _memory: GuestMemory, fd: int, flags: int) -> None:
        if flags & ~FDFLAGS_ALL:
            raise WasiError(ERRNO_INVAL)
        self.descriptor(fd, FD_FDSTAT_SET_FLAGS).flags = flags

    def w_fd_filestat_get(self, memory: GuestMemory, fd: int, pointer: int) -> None:
        self.filestat(memory, pointer, self.descriptor(fd, FD_FILESTAT_GET).node)

    def w_path_filestat_get(self, memory: GuestMemory, fd: int, _flags: int, pointer: int, length: int, result: int) -> None:
        self.descriptor(fd, PATH_FILESTAT_GET)
        self.filestat(memory, result, self.path(memory, fd, pointer, length).node)

    def w_path_open(self, memory: GuestMemory, fd: int, _lookup: int, pointer: int, length: int,
                    flags: int, rights: int, inheriting: int, fdflags: int, result: int) -> None:
        parent = self.descriptor(fd, PATH_OPEN)
        memory.check(result, 4)
        if rights & ~parent.inheriting or inheriting & ~parent.inheriting:
            raise WasiError(ERRNO_NOTCAPABLE)
        if flags & ~OFLAGS_ALL or fdflags & ~FDFLAGS_ALL or flags & (OFLAG_CREAT | OFLAG_DIRECTORY) == (OFLAG_CREAT | OFLAG_DIRECTORY):
            raise WasiError(ERRNO_INVAL)
        if flags & OFLAG_TRUNC and not rights & WRITE:
            raise WasiError(ERRNO_NOTCAPABLE)
        path = self.path(memory, fd, pointer, length, bool(flags & (OFLAG_CREAT | OFLAG_TRUNC) or rights & WRITE))
        if path.exists():
            if flags & OFLAG_CREAT and flags & OFLAG_EXCL:
                raise WasiError(ERRNO_EXIST)
        elif flags & OFLAG_CREAT:
            self.descriptor(fd, PATH_CREATE_FILE)
            if memory.read(pointer, length).endswith(b'/'):
                raise WasiError(ERRNO_NOENT)
            if self.created >= MAX_CREATED_NODES:
                raise WasiError(ERRNO_MFILE)
            path.write_bytes(b'')
            self.created += 1
        else:
            raise WasiError(ERRNO_NOENT)
        node = path.node
        if flags & OFLAG_DIRECTORY and not node.directory:
            raise WasiError(ERRNO_NOTDIR)
        if node.directory and (rights & WRITE or flags & OFLAG_TRUNC):
            raise WasiError(ERRNO_ISDIR)
        if flags & OFLAG_TRUNC:
            self.resize(node, 0)
        new_fd = self.next_fd
        self.next_fd += 1
        self.fds[new_fd] = Descriptor(node, rights, inheriting, parent.readonly, flags=fdflags)
        memory.pack('I', result, new_fd)

    def w_fd_close(self, _memory: GuestMemory, fd: int) -> None:
        self.descriptor(fd)
        del self.fds[fd]

    def transfer(self, memory: GuestMemory, fd: int, vectors: int, count: int, result: int,
                 write: bool, offset: Optional[int] = None) -> None:
        if not 0 <= count <= MAX_IOVECS:
            raise WasiError(ERRNO_INVAL)
        d = self.descriptor(fd, WRITE if write else READ)
        if d.node.directory:
            raise WasiError(ERRNO_ISDIR)
        memory.check(vectors, count * 8)
        memory.check(result, 4)
        chunks = [memory.unpack('II', vectors + index * 8) for index in range(count)]
        for pointer, size in chunks:
            memory.check(pointer, size)
        position = d.position if offset is None else offset
        if position < 0:
            raise WasiError(ERRNO_INVAL)
        if write and d.flags & FDFLAG_APPEND:
            position = len(d.node.data)
        total = 0
        for pointer, size in chunks:
            if write:
                data = memory.read(pointer, size)
                if size and not d.node.null:
                    self.resize(d.node, max(len(d.node.data), position + size))
                    d.node.data = d.node.data[:position] + data + d.node.data[position + size:]
            else:
                data = d.node.data[position:position + size]
                memory.write(pointer, data)
            transferred = size if write else len(data)
            total += transferred
            position += transferred
        if offset is None:
            d.position = position
        memory.pack('I', result, total)

    def w_fd_read(self, memory: GuestMemory, fd: int, vectors: int, count: int, result: int) -> None:
        self.transfer(memory, fd, vectors, count, result, False)

    def w_fd_write(self, memory: GuestMemory, fd: int, vectors: int, count: int, result: int) -> None:
        self.transfer(memory, fd, vectors, count, result, True)

    def w_fd_pread(self, memory: GuestMemory, fd: int, vectors: int, count: int, offset: int, result: int) -> None:
        self.descriptor(fd, FD_SEEK)
        self.transfer(memory, fd, vectors, count, result, False, offset)

    def w_fd_pwrite(self, memory: GuestMemory, fd: int, vectors: int, count: int, offset: int, result: int) -> None:
        self.descriptor(fd, FD_SEEK)
        self.transfer(memory, fd, vectors, count, result, True, offset)

    def w_fd_seek(self, memory: GuestMemory, fd: int, offset: int, whence: int, result: int) -> None:
        d = self.descriptor(fd, FD_SEEK)
        if whence not in (0, 1, 2):
            raise WasiError(ERRNO_INVAL)
        position = (0, d.position, len(d.node.data))[whence] + offset
        if position < 0:
            raise WasiError(ERRNO_INVAL)
        memory.pack('Q', result, 0 if d.node.null else position)
        d.position = 0 if d.node.null else position

    def w_fd_tell(self, memory: GuestMemory, fd: int, result: int) -> None:
        memory.pack('Q', result, self.descriptor(fd, FD_TELL).position)

    def w_fd_filestat_set_size(self, _memory: GuestMemory, fd: int, size: int) -> None:
        d = self.descriptor(fd, FD_FILESTAT_SET_SIZE)
        if d.node.directory:
            raise WasiError(ERRNO_ISDIR)
        if d.node.null:
            raise WasiError(ERRNO_INVAL)
        self.resize(d.node, size)

    @staticmethod
    def set_times(node: Node, atime: int, mtime: int, flags: int) -> None:
        if flags & ~FSTFLAGS_ALL or flags & FSTFLAGS_ATIME == FSTFLAGS_ATIME or flags & FSTFLAGS_MTIME == FSTFLAGS_MTIME:
            raise WasiError(ERRNO_INVAL)
        if flags & FSTFLAGS_ATIME:
            node.atime = time.time_ns() if flags & FSTFLAG_ATIM_NOW else atime & U64_MASK
        if flags & FSTFLAGS_MTIME:
            node.mtime = time.time_ns() if flags & FSTFLAG_MTIM_NOW else mtime & U64_MASK

    def w_fd_filestat_set_times(self, _memory: GuestMemory, fd: int, atime: int, mtime: int, flags: int) -> None:
        self.set_times(self.descriptor(fd, FD_FILESTAT_SET_TIMES).node, atime, mtime, flags)

    def w_path_filestat_set_times(self, memory: GuestMemory, fd: int, _lookup: int, pointer: int, length: int, atime: int, mtime: int, flags: int) -> None:
        self.descriptor(fd, PATH_FILESTAT_SET_TIMES)
        self.set_times(self.path(memory, fd, pointer, length, True).node, atime, mtime, flags)

    def w_fd_readdir(self, memory: GuestMemory, fd: int, buffer: int, length: int, cookie: int, result: int) -> None:
        d = self.descriptor(fd, FD_READDIR)
        if not d.node.directory:
            raise WasiError(ERRNO_NOTDIR)
        if cookie < 0:
            raise WasiError(ERRNO_INVAL)
        memory.check(buffer, length)
        entries = sorted(d.node.children.items())
        output = bytearray()
        for index in range(cookie, len(entries)):
            name, node = entries[index]
            encoded = name.encode('utf-8', errors='surrogateescape')
            output.extend(struct.pack('<QQIB3x', index + 1, node.inode, len(encoded), self.filetype(node)) + encoded)
            if len(output) >= length:
                break
        memory.write(buffer, bytes(output[:length]))
        memory.pack('I', result, min(length, len(output)))

    def w_path_create_directory(self, memory: GuestMemory, fd: int, pointer: int, length: int) -> None:
        self.descriptor(fd, PATH_CREATE_DIRECTORY)
        if self.created >= MAX_CREATED_NODES:
            raise WasiError(ERRNO_MFILE)
        self.path(memory, fd, pointer, length, True).mkdir()
        self.created += 1

    def w_path_unlink_file(self, memory: GuestMemory, fd: int, pointer: int, length: int) -> None:
        self.descriptor(fd, PATH_UNLINK_FILE)
        self.path(memory, fd, pointer, length, True).unlink()

    def w_path_remove_directory(self, memory: GuestMemory, fd: int, pointer: int, length: int) -> None:
        self.descriptor(fd, PATH_REMOVE_DIRECTORY)
        self.path(memory, fd, pointer, length, True).rmdir()

    def w_path_rename(self, memory: GuestMemory, old_fd: int, old_pointer: int, old_length: int,
                      new_fd: int, new_pointer: int, new_length: int) -> None:
        self.descriptor(old_fd, PATH_RENAME_SOURCE)
        self.descriptor(new_fd, PATH_RENAME_TARGET)
        old = self.path(memory, old_fd, old_pointer, old_length, True)
        new = self.path(memory, new_fd, new_pointer, new_length, True)
        # Directory descriptors can refer to subtrees, so prevent cycles by identity.
        if old.is_dir() and (new.parent.node is old.node or any(p.node is new.parent.node for p in old.walk())):
            raise WasiError(ERRNO_INVAL)
        old.replace(new)

    def w_fd_sync(self, _memory: GuestMemory, fd: int) -> None:
        self.descriptor(fd, FD_SYNC)

    def w_fd_datasync(self, _memory: GuestMemory, fd: int) -> None:
        self.descriptor(fd, FD_DATASYNC)

    def w_fd_advise(self, _memory: GuestMemory, fd: int, _offset: int, _length: int, advice: int) -> None:
        self.descriptor(fd, FD_ADVISE)
        if advice not in range(6):
            raise WasiError(ERRNO_INVAL)

    @staticmethod
    def clock(clock: int) -> int:
        if clock not in (0, 1, 2, 3):
            raise WasiError(ERRNO_INVAL)
        return (time.time_ns, time.monotonic_ns, time.process_time_ns, time.thread_time_ns)[clock]()

    def w_clock_time_get(self, memory: GuestMemory, clock: int, _precision: int, result: int) -> None:
        memory.pack('Q', result, self.clock(clock))

    def w_clock_res_get(self, memory: GuestMemory, clock: int, result: int) -> None:
        self.clock(clock)
        name: Literal['time', 'monotonic', 'process_time', 'thread_time'] = ('time', 'monotonic', 'process_time', 'thread_time')[clock]
        memory.pack('Q', result, max(1, round(time.get_clock_info(name).resolution * 1e9)))

    def w_random_get(self, memory: GuestMemory, pointer: int, length: int) -> None:
        memory.check(pointer, length)
        memory.write(pointer, os.urandom(length))

    def w_sched_yield(self, _memory: GuestMemory) -> None:
        time.sleep(0)

    def w_poll_oneoff(self, memory: GuestMemory, subscriptions: int, events: int, count: int, result: int) -> None:
        if not 0 < count <= MAX_SUBSCRIPTIONS:
            raise WasiError(ERRNO_INVAL)
        memory.check(subscriptions, count * 48)
        memory.check(events, count * 32)
        memory.check(result, 4)
        pending = []
        for index in range(count):
            pointer = subscriptions + index * 48
            userdata, kind = memory.unpack('QB', pointer)
            error, wait = ERRNO_SUCCESS, 0.0
            if kind == EVENTTYPE_CLOCK:
                clock, timeout, _precision, flags = memory.unpack('I4xQQH', pointer + 16)
                if flags & ~SUBCLOCK_ABSTIME:
                    raise WasiError(ERRNO_INVAL)
                now = self.clock(clock)
                wait = max(0, timeout - now if flags & SUBCLOCK_ABSTIME else timeout) / 1e9
            elif kind in (EVENTTYPE_FD_READ, EVENTTYPE_FD_WRITE):
                fd, = memory.unpack('I', pointer + 16)
                try:
                    self.descriptor(fd, READ if kind == EVENTTYPE_FD_READ else WRITE)
                except WasiError as exception:
                    error = exception.code
            else:
                raise WasiError(ERRNO_INVAL)
            pending.append((userdata, kind, error, wait))
        delay = min(item[3] for item in pending)
        deadline = time.monotonic() + delay
        while delay > 0:
            if self.stopped.wait(min(delay, 1.0)):
                break
            delay = max(0, deadline - time.monotonic())
        delay = min(item[3] for item in pending)
        ready = [item for item in pending if item[3] <= delay]
        for index, (userdata, kind, error, _wait) in enumerate(ready):
            memory.pack('QHB5xQH6x', events + index * 32, userdata, error, kind, 0, 0)
        memory.pack('I', result, len(ready))
