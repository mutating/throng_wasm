import struct
from threading import Event
from types import SimpleNamespace

import pytest
import wasmtime

from throng_wasm.memory import MemoryPath, Node
from throng_wasm.state import snapshot
from throng_wasm.wasi import (
    ALL_RIGHTS,
    READ,
    WRITE,
    Descriptor,
    GuestMemory,
    MemoryWasi,
)


class Harness:
    def __init__(self, store):
        self.root = MemoryPath()
        self.library = MemoryPath()
        (self.library / 'secret').write_bytes(b'readonly')
        self.wasi = MemoryWasi(['python', 'тест'], [('KEY', 'value')], [('/', self.root, False), ('/python', self.library, True)], Event(), 65536)
        self.raw = wasmtime.Memory(store, wasmtime.MemoryType(wasmtime.Limits(2, None)))
        self.caller = SimpleNamespace(get=lambda _: self.raw, _context=store._context)
        self.memory = GuestMemory(self.caller)

    def call(self, name, *args):
        return self.wasi.invoke(name, self.caller, *args)

    def path(self, name, address=256):
        data = name.encode()
        self.memory.write(address, data)
        return address, len(data)

    def open(self, name='file', flags=1, rights=ALL_RIGHTS, fd=3, fdflags=0):
        assert self.call('path_open', fd, 0, *self.path(name), flags, rights, 0, fdflags, 0) == 0
        return self.memory.unpack('I', 0)[0]

    def vectors(self, data=b'hello', length=None):
        self.memory.write(1024, data)
        self.memory.pack('II', 512, 1024, len(data) if length is None else length)
        return 512, 1, 128


@pytest.fixture
def h():
    with wasmtime.Engine() as engine, wasmtime.Store(engine) as store:
        yield Harness(store)


def test_strings_and_preopens(h):
    for prefix, expected in [('args', [b'python\0', 'тест\0'.encode()]), ('environ', [b'KEY=value\0'])]:
        assert h.call(prefix + '_sizes_get', 0, 4) == 0
        assert h.memory.unpack('II', 0) == (len(expected), sum(map(len, expected)))
        assert h.call(prefix + '_get', 16, 128) == 0
        assert h.memory.read(128, sum(map(len, expected))) == b''.join(expected)
        for index, value in enumerate(expected):
            pointer, = h.memory.unpack('I', 16 + index * 4)
            assert h.memory.read(pointer, len(value)) == value
    assert h.call('fd_prestat_get', 3, 0) == 0
    assert h.memory.unpack('II', 0) == (0, 1)
    assert h.call('fd_prestat_dir_name', 4, 128, 7) == 0
    assert h.memory.read(128, 7) == b'/python'
    assert h.call('fd_prestat_dir_name', 4, 128, 1) == 37
    assert h.call('fd_prestat_get', 1, 0) == 8
    assert h.call('fd_prestat_dir_name', 1, 0, 0) == 8
    assert h.call('fd_fdstat_get', 3, 0) == 0
    assert h.memory.unpack('BxH4xQQ', 0) == (3, 0, ALL_RIGHTS, ALL_RIGHTS)
    assert h.call('fd_fdstat_set_flags', 3, 32) == 28
    assert h.call('fd_fdstat_set_flags', 3, 1) == 0
    assert h.wasi.fds[3].flags == 1


def test_files_io_offsets_and_unlinked_handles(h):
    fd = h.open()
    assert h.call('fd_write', fd, *h.vectors()) == 0
    assert (h.root / 'file').read_bytes() == b'hello'
    assert h.call('fd_seek', fd, 0, 0, 128) == 0
    assert h.call('fd_read', fd, *h.vectors(b'xxxxx')) == 0
    assert h.memory.read(1024, 5) == b'hello'
    assert h.call('fd_tell', fd, 128) == 0
    assert h.memory.unpack('Q', 128) == (5,)
    h.vectors(b'Y')
    assert h.call('fd_pwrite', fd, 512, 1, 1, 128) == 0
    assert (h.root / 'file').read_bytes() == b'hYllo'
    h.vectors(b'xx')
    assert h.call('fd_pread', fd, 512, 1, 1, 128) == 0
    assert h.memory.read(1024, 2) == b'Yl'
    assert h.wasi.fds[fd].position == 5
    assert h.call('fd_seek', fd, -1, 2, 128) == 0
    assert h.memory.unpack('Q', 128) == (4,)
    assert h.call('fd_seek', fd, 0, 1, 128) == 0
    assert h.call('fd_seek', fd, -9, 0, 128) == 28
    assert h.call('fd_seek', fd, 0, 3, 128) == 28
    assert h.call('fd_pread', fd, 512, 1, -1, 128) == 28
    assert h.call('fd_filestat_set_size', fd, 2) == 0
    assert (h.root / 'file').read_bytes() == b'hY'
    assert h.call('fd_filestat_set_size', fd, 5) == 0
    assert (h.root / 'file').read_bytes() == b'hY\0\0\0'
    assert h.call('path_unlink_file', 3, *h.path('file')) == 0
    assert not (h.root / 'file').exists()
    assert h.call('fd_pread', fd, 512, 1, 0, 128) == 0
    assert h.memory.read(1024, 2) == b'hY'
    assert h.call('fd_close', fd) == 0
    assert h.call('fd_close', fd) == 8
    assert h.call('fd_tell', fd, 128) == 8


def test_append_truncate_null_and_multiple_vectors(h):
    fd = h.open(fdflags=1)
    assert h.call('fd_write', fd, *h.vectors(b'A')) == 0
    assert h.call('fd_seek', fd, 0, 0, 128) == 0
    assert h.call('fd_write', fd, *h.vectors(b'B')) == 0
    assert (h.root / 'file').read_bytes() == b'AB'
    h.open(flags=8)
    assert (h.root / 'file').read_bytes() == b''
    h.memory.pack('IIII', 512, 1024, 1, 1025, 1)
    h.memory.write(1024, b'CD')
    assert h.call('fd_write', fd, 512, 2, 128) == 0
    assert (h.root / 'file').read_bytes() == b'CD'
    sparse = h.open('sparse')
    assert h.call('fd_seek', sparse, 100, 0, 128) == 0
    assert h.call('fd_write', sparse, *h.vectors(b'')) == 0
    assert (h.root / 'sparse').read_bytes() == b''
    null_fd = h.wasi.next_fd
    h.wasi.fds[null_fd] = Descriptor(Node(null=True))
    assert h.call('fd_write', null_fd, *h.vectors(b'ignored')) == 0
    assert not h.wasi.fds[null_fd].node.data
    assert h.call('fd_seek', null_fd, 12, 0, 128) == 0
    assert h.memory.unpack('Q', 128) == (0,)
    assert h.call('fd_filestat_set_size', null_fd, 10) == 28
    assert h.call('fd_read', null_fd, *h.vectors(b'xxxx')) == 0
    assert h.memory.unpack('I', 128) == (0,)
    assert h.call('fd_fdstat_get', null_fd, 0) == 0
    assert h.memory.read(0, 1) == b'\x02'


def test_stat_and_timestamps(h):
    fd = h.open()
    assert h.call('fd_filestat_get', fd, 0) == 0
    stat = h.memory.unpack('QQB7xQQQQQ', 0)
    assert stat[2:5] == (4, 1, 0)
    assert h.call('fd_filestat_set_times', fd, 100, 200, 1 | 4) == 0
    assert h.call('path_filestat_get', 3, 0, *h.path('file'), 0) == 0
    assert h.memory.unpack('QQB7xQQQQQ', 0)[5:] == (100, 200, 200)
    assert h.call('path_filestat_set_times', 3, 0, *h.path('file'), 0, 0, 2 | 8) == 0
    assert (h.root / 'file').node.atime > 100
    assert (h.root / 'file').node.mtime > 200
    assert h.call('fd_filestat_set_times', fd, 1, 2, 0) == 0
    for flags in [3, 12, 16]:
        assert h.call('fd_filestat_set_times', fd, 1, 2, flags) == 28
    assert h.call('fd_datasync', fd) == 0
    assert h.call('fd_sync', fd) == 0
    assert h.call('fd_advise', fd, 0, 0, 0) == 0
    assert h.call('fd_advise', fd, 0, 0, 6) == 28


def test_directories_rename_and_readdir(h):
    assert h.call('path_create_directory', 3, *h.path('dir')) == 0
    assert h.call('path_create_directory', 3, *h.path('dir')) == 20
    h.open('dir/file')
    assert h.call('path_remove_directory', 3, *h.path('dir')) == 55
    assert h.call('path_unlink_file', 3, *h.path('dir')) == 31
    old = h.path('dir', 256)
    new = h.path('renamed', 300)
    assert h.call('path_rename', 3, *old, 3, *new) == 0
    assert (h.root / 'renamed/file').is_file()
    old = h.path('renamed', 256)
    new = h.path('renamed/sub', 300)
    assert h.call('path_rename', 3, *old, 3, *new) == 28
    assert h.call('fd_readdir', 3, 512, 100, 0, 128) == 0
    size, = h.memory.unpack('I', 128)
    assert h.memory.unpack('QQIB3x', 512)[0::2] == (1, 7)
    assert h.memory.read(536, size - 24) == b'renamed'
    assert h.call('fd_readdir', 3, 512, 3, 0, 128) == 0
    assert h.memory.unpack('I', 128) == (3,)
    assert h.call('fd_readdir', 3, 512, 100, 1, 128) == 0
    assert h.memory.unpack('I', 128) == (0,)
    assert h.call('fd_readdir', 3, 512, 100, -1, 128) == 28
    assert h.call('fd_readdir', 3, 512, -1, 0, 128) == 21
    assert h.call('path_unlink_file', 3, *h.path('renamed/file')) == 0
    assert h.call('path_remove_directory', 3, *h.path('renamed')) == 0


@pytest.mark.parametrize(('name', 'code'), [('../escape', 76), ('/absolute', 76), ('', 28), ('\x00bad', 28), ('missing', 44)])
def test_bad_guest_paths(h, name, code):
    assert h.call('path_open', 3, 0, *h.path(name), 0, READ, 0, 0, 0) == code


def test_access_checks_and_bad_open(h):
    fd = h.open(rights=READ)
    assert h.call('fd_write', fd, *h.vectors()) == 76
    assert h.call('path_open', 4, 0, *h.path('secret'), 0, WRITE, 0, 0, 0) == 76
    assert h.call('path_open', 4, 0, *h.path('secret'), 0, READ, ALL_RIGHTS, 0, 0) == 76
    assert h.call('path_open', 4, 0, *h.path('new'), 1, READ, 0, 0, 0) == 69
    assert h.call('path_filestat_set_times', 4, 0, *h.path('secret'), 1, 2, 0) == 76
    assert h.call('path_open', fd, 0, *h.path('x'), 0, 0, 0, 0, 0) == 76
    h.wasi.fds[fd].rights = ALL_RIGHTS
    assert h.call('path_open', fd, 0, *h.path('x'), 0, 0, 0, 0, 0) == 54
    for flags, fdflags, rights, code in [(16, 0, READ, 28), (0, 32, READ, 28), (5, 0, READ, 20), (2, 0, READ, 54), (8, 0, READ, 76)]:
        assert h.call('path_open', 3, 0, *h.path('file'), flags, rights, 0, fdflags, 0) == code
    assert h.call('path_open', 3, 0, *h.path('.'), 0, WRITE, 0, 0, 0) == 31
    assert h.call('fd_read', 3, *h.vectors()) == 31
    assert h.call('fd_filestat_set_size', 3, 0) == 31
    assert h.call('fd_readdir', fd, 512, 10, 0, 128) == 54
    h.wasi.created = 65536
    assert h.call('path_open', 3, 0, *h.path('another'), 1, READ, 0, 0, 0) == 33


def test_memory_bounds_and_limits(h, monkeypatch):
    assert h.call('args_sizes_get', -1, 0) == 21
    assert h.call('args_sizes_get', 131071, 0) == 21
    assert h.call('random_get', 0, -1) == 21
    assert h.call('random_get', 131072, 0) == 0
    assert h.memory.read(131072, 0) == b''
    bad = SimpleNamespace(get=lambda _: None)
    assert h.wasi.invoke('random_get', bad, 0, 1) == 21
    fd = h.open()
    assert h.call('fd_write', fd, 0, 100000, 0) == 28
    h.memory.pack('II', 512, 131071, 5)
    assert h.call('fd_write', fd, 512, 1, 128) == 21
    assert h.call('fd_filestat_set_size', fd, -1) == 22
    assert h.call('fd_filestat_set_size', fd, 65537) == 22
    assert h.call('fd_filestat_set_size', fd, 65536) == 0
    other = h.open('other')
    assert h.call('fd_filestat_set_size', other, 1) == 48
    def fail(*_args):
        raise MemoryError
    monkeypatch.setattr(h.wasi, 'w_random_get', fail)
    assert h.call('random_get', 0, 1) == 48
    def io_error(*_args):
        raise OSError('unknown host errno')
    monkeypatch.setattr(h.wasi, 'w_random_get', io_error)
    assert h.call('random_get', 0, 1) == 29


def test_clocks_random_exit_and_unsupported_calls(h):
    for clock in range(4):
        assert h.call('clock_time_get', clock, 0, 0) == 0
        assert h.memory.unpack('Q', 0)[0] > 0
        assert h.call('clock_res_get', clock, 0) == 0
        assert h.memory.unpack('Q', 0)[0] >= 1
    assert h.call('clock_time_get', 4, 0, 0) == 28
    assert h.call('sched_yield') == 0
    assert h.call('random_get', 512, 32) == 0
    assert h.memory.read(512, 32) != b'\0' * 32
    for name in ['path_link', 'path_symlink', 'path_readlink', 'sock_accept', 'sock_send', 'sock_recv', 'sock_shutdown', 'unknown']:
        assert h.call(name, 0) == 58


def test_poll(h):
    h.memory.write(256, struct.pack('<QB7xI4xQQH6x', 123, 0, 1, 1000, 0, 0))
    assert h.call('poll_oneoff', 256, 512, 1, 128) == 0
    assert h.memory.unpack('QHB5xQH6x', 512) == (123, 0, 0, 0, 0)
    assert h.memory.unpack('I', 128) == (1,)
    h.memory.write(256, struct.pack('<QB7xI4xQQH6x', 124, 0, 1, 0, 0, 1))
    assert h.call('poll_oneoff', 256, 512, 1, 128) == 0
    for kind, fd, code in [(1, 0, 0), (2, 1, 0), (1, 999, 8), (1, 1, 76)]:
        h.memory.write(256, struct.pack('<QB7xI28x', 125, kind, fd))
        assert h.call('poll_oneoff', 256, 512, 1, 128) == 0
        assert h.memory.unpack('QHB5xQH6x', 512)[1] == code
    assert h.call('poll_oneoff', 256, 512, 0, 128) == 28
    h.memory.write(256, struct.pack('<QB7xI4xQQH6x', 0, 0, 1, 0, 0, 2))
    assert h.call('poll_oneoff', 256, 512, 1, 128) == 28
    h.memory.write(256, struct.pack('<QB39x', 0, 9))
    assert h.call('poll_oneoff', 256, 512, 1, 128) == 28


def test_reject_unrelated_imports():
    with wasmtime.Engine() as engine, wasmtime.Linker(engine) as linker:
        wasi = MemoryWasi([], [], [], Event(), 1024)
        for source in ['(module (import "host" "func" (func)))', '(module (import "wasi_snapshot_preview1" "memory" (memory 1)))']:
            with wasmtime.Module(engine, source) as module, pytest.raises(ValueError, match='Unsupported WASM import'):
                wasi.link(linker, module)


def test_path_limits_and_directory_budget(h):
    assert h.call('path_open', 3, 0, *h.path('x' * 4097), 1, READ, 0, 0, 0) == 37
    assert h.call('path_open', 3, 0, *h.path('x' * 256), 1, READ, 0, 0, 0) == 37
    assert h.call('path_open', 3, 0, *h.path('bad-directory'), 3, READ, 0, 0, 0) == 28
    assert not (h.root / 'bad-directory').exists()
    h.open('file')
    assert h.call('path_open', 3, 0, *h.path('file/'), 0, READ, 0, 0, 0) == 54
    h.wasi.created = 65536
    assert h.call('path_create_directory', 3, *h.path('dir')) == 33


def test_sleep_can_be_interrupted(h):
    h.wasi.stopped.set()
    h.memory.write(256, struct.pack('<QB7xI4xQQH6x', 123, 0, 1, 1000000000000000, 0, 0))
    assert h.call('poll_oneoff', 256, 512, 1, 128) == 0
    assert h.memory.unpack('I', 128) == (1,)


def test_path_components_and_rename_through_another_descriptor(h):
    h.open('file')
    for path, code in [('missing/../file', 44), ('file/../file', 54), ('file/.', 54), ('missing/', 44)]:
        assert h.call('path_open', 3, 0, *h.path(path), 1, READ, 0, 0, 0) == code
    assert not (h.root / 'missing').exists()
    assert h.call('path_create_directory', 3, *h.path('dir/')) == 0
    h.open('dir/file')
    directory = h.open('dir', flags=2, rights=ALL_RIGHTS & ~WRITE)
    original = (h.root / 'dir/file').node
    assert h.call('path_rename', 3, *h.path('dir/file', 256), directory, *h.path('file', 300)) == 0
    assert (h.root / 'dir/file').node is original


def test_multivector_partial_reads_eof_and_sparse_write(h):
    fd = h.open()
    assert h.call('fd_write', fd, *h.vectors(b'abc')) == 0
    assert h.call('fd_seek', fd, 0, 0, 128) == 0
    h.memory.pack('IIII', 512, 1024, 2, 2048, 4)
    h.memory.write(1024, b'XX')
    h.memory.write(2048, b'YYYY')
    assert h.call('fd_read', fd, 512, 2, 128) == 0
    assert h.memory.unpack('I', 128) == (3,)
    assert h.memory.read(1024, 2) == b'ab'
    assert h.memory.read(2048, 4) == b'cYYY'
    assert h.call('fd_read', fd, 512, 2, 128) == 0
    assert h.memory.unpack('I', 128) == (0,)
    assert h.memory.read(2048, 4) == b'cYYY'
    assert h.call('fd_write', fd, 131072, 0, 128) == 0
    assert h.wasi.fds[fd].position == 3
    assert h.call('fd_seek', fd, 8, 0, 128) == 0
    assert h.call('fd_write', fd, *h.vectors(b'Z')) == 0
    assert (h.root / 'file').read_bytes() == b'abc' + b'\0' * 5 + b'Z'


@pytest.mark.parametrize('operation', ['fd_read', 'fd_write'])
def test_invalid_second_iovec_has_no_partial_effect(h, operation):
    fd = h.open()
    (h.root / 'file').write_bytes(b'original')
    h.memory.write(1024, b'unchanged')
    h.memory.pack('IIII', 512, 1024, 4, 131071, 2)
    assert h.call(operation, fd, 512, 2, 128) == 21
    assert (h.root / 'file').read_bytes() == b'original'
    assert h.memory.read(1024, 9) == b'unchanged'
    assert h.wasi.fds[fd].position == 0


def test_invalid_result_pointer_does_not_create_or_truncate(h):
    descriptors = set(h.wasi.fds)
    assert h.call('path_open', 3, 0, *h.path('new'), 1, ALL_RIGHTS, 0, 0, 131071) == 21
    assert not (h.root / 'new').exists()
    assert set(h.wasi.fds) == descriptors
    h.open('existing')
    descriptors = set(h.wasi.fds)
    (h.root / 'existing').write_bytes(b'keep')
    assert h.call('path_open', 3, 0, *h.path('existing'), 8, ALL_RIGHTS, 0, 0, -1) == 21
    assert (h.root / 'existing').read_bytes() == b'keep'
    assert set(h.wasi.fds) == descriptors


@pytest.mark.parametrize(('operation', 'right'), [
    ('fd_write', WRITE), ('fd_filestat_set_size', 1 << 22),
    ('fd_filestat_set_times', 1 << 23), ('fd_fdstat_set_flags', 1 << 3),
    ('path_create_directory', 1 << 9), ('path_unlink_file', 1 << 26),
    ('path_remove_directory', 1 << 25), ('path_filestat_set_times', 1 << 20),
    ('path_rename_source', 1 << 16), ('path_rename_destination', 1 << 17),
    ('path_open', 1 << 13), ('path_open_create', 1 << 10),
])
def test_mutating_syscalls_require_their_own_right(h, operation, right):
    fd = h.open()
    assert h.call('fd_write', fd, *h.vectors(b'keep')) == 0
    assert h.call('path_create_directory', 3, *h.path('directory')) == 0
    before = snapshot(h.root)
    h.wasi.fds[fd if operation.startswith('fd_') else 3].rights &= ~right
    name = h.path('file', 256)
    other = h.path('other', 300)
    calls = {
        'fd_write': ('fd_write', fd, *h.vectors(b'bad')),
        'fd_filestat_set_size': ('fd_filestat_set_size', fd, 0),
        'fd_filestat_set_times': ('fd_filestat_set_times', fd, 1, 2, 5),
        'fd_fdstat_set_flags': ('fd_fdstat_set_flags', fd, 1),
        'path_create_directory': ('path_create_directory', 3, *other),
        'path_unlink_file': ('path_unlink_file', 3, *name),
        'path_remove_directory': ('path_remove_directory', 3, *h.path('directory', 350)),
        'path_filestat_set_times': ('path_filestat_set_times', 3, 0, *name, 1, 2, 5),
        'path_rename_source': ('path_rename', 3, *name, 3, *other),
        'path_rename_destination': ('path_rename', 3, *name, 3, *other),
        'path_open': ('path_open', 3, 0, *name, 0, READ, 0, 0, 0),
        'path_open_create': ('path_open', 3, 0, *other, 1, READ, 0, 0, 0),
    }
    assert h.call(*calls[operation]) == 76
    assert snapshot(h.root) == before
    assert h.wasi.fds[fd].flags == 0


def test_directory_descriptor_cannot_escalate_or_escape(h):
    assert h.call('path_create_directory', 3, *h.path('dir')) == 0
    h.open('outside')
    h.open('dir/inside')
    assert h.call('path_open', 3, 0, *h.path('dir'), 2, 1 << 13, READ, 0, 0) == 0
    directory, = h.memory.unpack('I', 0)
    assert h.call('path_open', directory, 0, *h.path('inside'), 0, WRITE, 0, 0, 0) == 76
    assert h.call('path_open', directory, 0, *h.path('inside'), 0, READ, WRITE, 0, 0) == 76
    assert h.call('path_open', directory, 0, *h.path('../outside'), 0, READ, 0, 0, 0) == 76
    assert h.call('path_open', directory, 0, *h.path('inside'), 0, READ, 0, 0, 0) == 0


def test_readdir_cookies_unicode_and_zero_length(h):
    for name in ['z', 'a', 'я']:
        h.open(name)
    for cookie, name in enumerate(['a', 'z', 'я']):
        size = 24 + len(name.encode())
        assert h.call('fd_readdir', 3, 512, size, cookie, 128) == 0
        next_cookie, inode, length, kind = h.memory.unpack('QQIB3x', 512)
        assert (next_cookie, length, kind) == (cookie + 1, len(name.encode()), 4)
        assert inode == (h.root / name).node.inode
        assert h.memory.read(536, length) == name.encode()
    assert h.call('fd_readdir', 3, 131072, 0, 0, 128) == 0
    assert h.memory.unpack('I', 128) == (0,)
    assert h.call('fd_readdir', 3, 512, 100, 999, 128) == 0
    assert h.memory.unpack('I', 128) == (0,)


def test_rename_over_open_file_and_prevent_descriptor_cycle(h):
    old_fd, new_fd = h.open('old'), h.open('new')
    assert h.call('fd_write', old_fd, *h.vectors(b'old')) == 0
    assert h.call('fd_write', new_fd, *h.vectors(b'new')) == 0
    assert h.call('path_rename', 3, *h.path('old', 256), 3, *h.path('new', 300)) == 0
    assert h.wasi.fds[old_fd].node is (h.root / 'new').node
    assert h.wasi.fds[new_fd].node.data == b'new'
    assert h.call('path_create_directory', 3, *h.path('dir')) == 0
    assert h.call('path_create_directory', 3, *h.path('dir/sub')) == 0
    directory = h.open('dir/sub', flags=2, rights=ALL_RIGHTS & ~WRITE)
    before = snapshot(h.root)
    assert h.call('path_rename', 3, *h.path('dir', 256), directory, *h.path('cycle', 300)) == 28
    assert snapshot(h.root) == before


def test_files_and_output_share_budget_and_shrink_releases_it(h):
    h.wasi.limit = 8
    fd = h.open()
    assert h.call('fd_write', fd, *h.vectors(b'12345678')) == 0
    assert h.call('fd_write', 1, *h.vectors(b'!')) == 48
    assert h.wasi.stdout.data == b''
    assert h.call('fd_filestat_set_size', fd, 4) == 0
    assert h.call('fd_write', 2, *h.vectors(b'abcd')) == 0
    assert h.wasi.stderr.data == b'abcd'
    assert h.call('fd_write', 1, *h.vectors(b'!')) == 48
    assert h.wasi.used == 8
    assert (h.root / 'file').read_bytes() == b'1234'


def test_poll_multiple_subscriptions_and_ready_userdata(h):
    subscriptions = [
        struct.pack('<QB7xI4xQQH6x', 10, 0, 1, 1000000000000, 0, 0),
        struct.pack('<QB7xI28x', 20, 1, 0),
        struct.pack('<QB7xI28x', 30, 2, 1),
    ]
    h.memory.write(256, b''.join(subscriptions))
    assert h.call('poll_oneoff', 256, 512, 3, 128) == 0
    assert h.memory.unpack('I', 128) == (2,)
    assert h.memory.unpack('QHB5xQH6x', 512)[:3] == (20, 0, 1)
    assert h.memory.unpack('QHB5xQH6x', 544)[:3] == (30, 0, 2)
    h.memory.write(256, b''.join(struct.pack('<QB7xI4xQQH6x', ident, 0, 1, 0, 0, 1) for ident in [40, 50]))
    assert h.call('poll_oneoff', 256, 512, 2, 128) == 0
    assert h.memory.unpack('I', 128) == (2,)
    assert h.memory.unpack('Q', 512) == (40,)
    assert h.memory.unpack('Q', 544) == (50,)
    h.memory.write(304, struct.pack('<QB39x', 60, 9))
    assert h.call('poll_oneoff', 256, 512, 2, 128) == 28
