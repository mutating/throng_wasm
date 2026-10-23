"""Exclusion contracts shared by host snapshots and in-memory isolate snapshots."""

import builtins
import io
import os
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Union
from zipfile import ZipFile

import pytest
from cantok import CancellationError, SimpleToken
from throng import throng

from throng_wasm import WasmIsolate, WasmManager, WasmRuntime
from throng_wasm.memory import MemoryPath
from throng_wasm.state import DEFAULT_EXCLUDE, restore, snapshot


@pytest.mark.parametrize('storage', ['absolute', 'relative', 'dot', 'memory'])
@pytest.mark.parametrize(('patterns', 'paths', 'kept'), [
    ([], ['a', 'nested/b', 'empty/'], ['a', 'nested/b', 'empty/']),
    (['missing'], ['a', 'nested/b'], ['a', 'nested/b']),
    (['secret'], ['secret', 'a/secret', 'secret.txt', 'secrets/a'], ['secret.txt', 'secrets/a']),
    (['/secret'], ['secret', 'a/secret'], ['a/secret']),
    (['./secret'], ['secret', 'a/secret'], ['secret', 'a/secret']),
    (['src/secret'], ['src/secret', 'a/src/secret', 'src/secret.txt'], ['a/src/secret', 'src/secret.txt']),
    (['build'], ['build/a', 'nested/build/b', 'builder/c'], ['builder/c']),
    (['build/'], ['build/a', 'nested/build/b', 'other/build', 'build.py'], ['other/build', 'build.py']),
    (['/build/'], ['build/a', 'nested/build/b', 'other/build'], ['nested/build/b', 'other/build']),
    (['*.tmp'], ['a.tmp', '.hidden.tmp', 'src/b.tmp', 'folder.tmp/child', 'tmp', 'a.tmpx'], ['tmp', 'a.tmpx']),
    (['/a.tmp'], ['a.tmp', 'src/a.tmp', 'b.tmp'], ['src/a.tmp', 'b.tmp']),
    (['src/*.py'], ['src/a.py', 'src/deep/a.py', 'other/src/a.py', 'src/a.txt'], ['src/deep/a.py', 'other/src/a.py', 'src/a.txt']),
    (['test?.py'], ['test1.py', 'nested/testA.py', 'test.py', 'test12.py'], ['test.py', 'test12.py']),
    (['test[0-2].py'], ['test0.py', 'nested/test2.py', 'test3.py', 'testa.py'], ['test3.py', 'testa.py']),
    (['test[!0-2].py'], ['test0.py', 'test2.py', 'test3.py', 'testa.py'], ['test0.py', 'test2.py']),
    (['**/secret'], ['secret', 'a/secret', 'a/b/secret', 'public'], ['public']),
    (['src/**/secret'], ['src/secret', 'src/a/secret', 'src/a/b/secret', 'other/src/secret'], ['other/src/secret']),
    (['build/**'], ['build/', 'build/a', 'build/nested/b', 'other/build/c'], ['other/build/c']),
    (['**/*.py'], ['a.py', 'nested/b.py', '.hidden.py', 'a.pyx'], ['a.pyx']),
    (['*'], ['a', '.hidden', 'nested/b', 'empty/'], []),
    (['*.py', '!keep.py'], ['a.py', 'keep.py', 'nested/keep.py', 'keep.pyx'], ['keep.py', 'nested/keep.py', 'keep.pyx']),
    (['!keep.py', '*.py'], ['a.py', 'keep.py', 'keep.txt'], ['keep.txt']),
    (['*.py', '!keep.py', 'keep.py'], ['keep.py', 'keep.txt'], ['keep.txt']),
    (['build/', '!build/keep.py'], ['build/drop.py', 'build/keep.py', 'other/build/keep.py'], ['build/keep.py']),
    (['build/', '!build/'], ['build/a', 'build/deep/b', 'other/build/c'], ['build/a', 'build/deep/b', 'other/build/c']),
    (['*', '!**/*.py'], ['a.py', 'a.txt', 'src/b.py', 'src/b.txt', 'empty/'], ['a.py', 'src/b.py']),
    (['*', '!*/'], ['a', 'src/b', 'src/deep/c', 'empty/'], ['src/b', 'src/deep/c', 'empty/']),
    (['*.tmp', '*.tmp'], ['a.tmp', 'keep'], ['keep']),
    (['', '# comment', '  '], ['a', '.hidden'], ['a', '.hidden']),
    ([r'\#secret'], ['#secret', 'nested/#secret', 'secret'], ['secret']),
    ([r'\!secret'], ['!secret', 'nested/!secret', 'secret'], ['secret']),
    (['data file.txt'], ['data file.txt', 'nested/data file.txt', 'datafile.txt'], ['datafile.txt']),
    (['данные/'], ['данные/секрет', 'nested/данные/секрет', 'data/секрет'], ['data/секрет']),
    (['secret'], ['SECRET', 'nested/Secret'], ['SECRET', 'nested/Secret']),
    (['a.tmp   '], ['a.tmp', 'keep'], ['keep']),
    (['../outside'], ['outside', 'nested/outside'], ['outside', 'nested/outside']),
    (['empty/'], ['empty/', 'nested/empty/', 'other/empty'], ['other/empty']),
    (['empty/', '!nested/empty/'], ['empty/', 'nested/empty/'], ['nested/empty/']),
    (['/'], ['a', 'nested/b'], ['a', 'nested/b']),
], ids=[
    'empty', 'no-match', 'name-any-depth', 'root-file', 'dot-is-literal', 'relative-path',
    'directory-name', 'directory-only', 'root-directory', 'star', 'root-extension', 'star-one-level',
    'question', 'character-class', 'negated-class', 'double-star-prefix', 'double-star-middle',
    'double-star-suffix', 'double-star-extension', 'exclude-all', 'reinclude-file', 'last-rule-wins',
    'exclude-again', 'reinclude-child', 'reinclude-tree', 'allowlist', 'reinclude-directory-descendants', 'duplicates', 'comments',
    'escaped-hash', 'escaped-bang', 'spaces', 'unicode', 'case-sensitive', 'trailing-spaces',
    'parent-path', 'empty-directories', 'reinclude-empty-directory', 'root-slash',
])
def test_pattern_semantics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: str,  # noqa: PLR0913
                           patterns: List[str], paths: List[str], kept: List[str]) -> None:
    # Deliberately place the project under a name also commonly excluded. Rules
    # must see only paths inside the project, never its host ancestors.
    root: Union[Path, MemoryPath]
    if storage == 'memory':
        root = MemoryPath() / 'build' / 'project'
        root.mkdir(parents=True)
    else:
        host_root = tmp_path / 'build' / 'project'
        host_root.mkdir(parents=True)
        if storage == 'absolute':
            root = host_root
        elif storage == 'relative':
            monkeypatch.chdir(tmp_path)
            root = Path('build/project')
        else:
            monkeypatch.chdir(host_root)
            root = Path()
    for name in paths:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith('/'):
            target.mkdir(exist_ok=True)
        else:
            target.write_bytes(name.encode())
    state = snapshot(root, patterns)
    restored = restore(state)
    with ZipFile(BytesIO(state)) as archive:
        archived = set(archive.namelist())
    for name in paths:
        assert (name in archived) == (name in kept), (storage, patterns, name, archived)
        if name in kept:
            assert (restored / name).exists()
            if not name.endswith('/'):
                assert (restored / name).read_bytes() == name.encode()
        elif not name.endswith('/'):
            assert not (restored / name).exists()


@pytest.mark.parametrize('entry', ['slot-default', 'slot-str', 'slot-path', 'slot-positional', 'manager', 'manager-positional', 'manager-none'])
@pytest.mark.parametrize('patterns', [None, [], ['private/', '*.tmp', '!keep.tmp']])
def test_public_api_and_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, patterns: Optional[List[str]]) -> None:
    monkeypatch.chdir(tmp_path)
    for name in [*(f'{item}/secret' for item in DEFAULT_EXCLUDE), 'private/secret', 'drop.tmp', 'keep.tmp', 'main.py']:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('data')
    if entry == 'slot-default':
        manager = throng(exclude=patterns)['wasm']
    elif entry == 'slot-str':
        manager = throng(str(tmp_path), exclude=patterns)['wasm']
    elif entry == 'slot-path':
        manager = throng(tmp_path, exclude=patterns)['wasm']
    elif entry == 'slot-positional':
        manager = throng(tmp_path, patterns)['wasm']
    elif entry == 'manager':
        manager = WasmManager(tmp_path, exclude=patterns)
    elif entry == 'manager-positional':
        manager = WasmManager(tmp_path, patterns)
    else:
        manager = WasmManager(None, exclude=patterns)
    assert isinstance(manager, WasmManager)
    initial = restore(manager.read())
    for name in DEFAULT_EXCLUDE:
        assert (initial / name).exists() == (patterns is not None)
    assert (initial / 'main.py').is_file()
    assert (initial / 'keep.tmp').is_file()
    assert (initial / 'private').exists() == (not patterns)
    assert (initial / 'drop.tmp').exists() == (not patterns)


def test_configuration_is_copied_for_each_manager_and_isolate(tmp_path: Path, runtime: WasmRuntime) -> None:
    patterns = ['private']
    manager = WasmManager(tmp_path, patterns, runtime=runtime)
    patterns.clear()
    assert manager.exclude == ['private']
    with manager.scope as isolate:
        assert isinstance(isolate, WasmIsolate)
        assert manager.exclude is not None
        manager.exclude.append('later')
        (isolate.path / 'private').write_text('hidden')
        (isolate.path / 'later').write_text('kept')
        saved = restore(isolate.read())
        assert not (saved / 'private').exists()
        assert (saved / 'later').read_text() == 'kept'
        assert (isolate.path / 'private').read_text() == 'hidden'
    other = WasmManager(tmp_path, exclude=[])
    assert other.exclude == []


@pytest.mark.parametrize('patterns', [None, [], ['*.tmp'], ['*', '!keep']])
def test_isolate_snapshot_and_clone_apply_manager_rules(tmp_path: Path, runtime: WasmRuntime, patterns: Optional[List[str]]) -> None:
    manager = WasmManager(tmp_path, exclude=patterns, runtime=runtime)
    with manager.scope as isolate:
        assert isinstance(isolate, WasmIsolate)
        (isolate.path / '.mypy_cache').mkdir()
        (isolate.path / '.mypy_cache/data').write_bytes(b'cache')
        (isolate.path / 'generated.tmp').write_bytes(b'temporary')
        (isolate.path / 'keep').write_bytes(b'kept')
        for _ in range(2):
            state = isolate.read()
            clone = manager.get(state)
            try:
                assert (clone.path / 'keep').read_bytes() == b'kept'
                assert (clone.path / 'generated.tmp').exists() == (patterns is None or patterns == [])
                assert (clone.path / '.mypy_cache/data').exists() == (patterns in ([], ['*.tmp']))
                (clone.path / 'regenerated.tmp').write_bytes(b'temporary')
                saved = restore(clone.read())
                assert (saved / 'regenerated.tmp').exists() == (patterns is None or patterns == [])
            finally:
                clone.kill()
        assert (isolate.path / '.mypy_cache/data').read_bytes() == b'cache'
        assert (isolate.path / 'generated.tmp').read_bytes() == b'temporary'


@pytest.mark.parametrize('patterns', [['*'], ['**'], ['.throng-wasm/'], ['packages/', '*.py', '*.json']])
def test_snapshot_exclusions_never_strip_installed_environment(patterns: List[str]) -> None:
    project = MemoryPath()
    (project / 'main.py').write_bytes(b'project')
    environment = MemoryPath() / '.throng-wasm'
    (environment / 'packages/tool').mkdir(parents=True)
    (environment / 'packages/tool/__init__.py').write_bytes(b'package')
    (environment / 'manifest.json').write_bytes(b'manifest')
    saved = restore(snapshot(project, patterns, extra=[environment]))
    assert (saved / '.throng-wasm/packages/tool/__init__.py').read_bytes() == b'package'
    assert (saved / '.throng-wasm/manifest.json').read_bytes() == b'manifest'


def test_snapshot_never_opens_excluded_files_and_preserves_empty_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    private = tmp_path / 'private'
    private.mkdir()
    (private / 'secret').write_bytes(b'never read')
    (tmp_path / 'visible').write_bytes(b'visible')
    (tmp_path / 'empty').mkdir()

    def guard(open_file, file, *args, **kwargs):
        if not isinstance(file, int):
            path = Path(os.fsdecode(file)).absolute()
            assert path != private, f'Opened excluded path: {path}'
            assert private not in path.parents, f'Opened excluded path: {path}'
        return open_file(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        for module in [builtins, io, os]:
            patch.setattr(module, 'open', partial(guard, module.open))
        state = snapshot(tmp_path, ['/private/'])
    saved = restore(state)
    assert (saved / 'visible').read_bytes() == b'visible'
    assert not (saved / 'private').exists()
    assert (saved / 'empty').is_dir()


@pytest.mark.parametrize('storage', ['host', 'memory'])
@pytest.mark.parametrize('exception', ['cancel', 'callback', 'one-shot-cancel'])
def test_cancellation_and_callback_errors_during_fully_excluded_walk(tmp_path: Path, storage: str, exception: str) -> None:
    root: Union[Path, MemoryPath] = tmp_path if storage == 'host' else MemoryPath()
    for i in range(20):
        (root / str(i)).write_bytes(b'hidden')
    token = SimpleToken()
    checks = []
    failure = CancellationError('cancelled once', token) if exception == 'one-shot-cancel' else RuntimeError('callback failed')

    def check():
        checks.append(1)
        if len(checks) == 5:
            if exception != 'cancel':
                raise failure
            token.cancel()
        token.check()

    with pytest.raises(RuntimeError if exception == 'callback' else CancellationError) as caught:
        snapshot(root, ['*'], check=check)
    assert len(checks) == 5
    if exception != 'cancel':
        assert caught.value is failure


def test_parallel_snapshots_keep_independent_roots_and_rules(tmp_path: Path) -> None:
    projects = []
    for name in ['first', 'second']:
        root = tmp_path / name
        root.mkdir()
        (root / 'private').write_text(name)
        (root / 'public').write_text(name)
        projects.append(root)
    original_cwd = Path.cwd()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda pair: restore(snapshot(*pair)), zip(projects, [['/private'], ['/public']])))
    assert Path.cwd() == original_cwd
    assert (results[0] / 'public').read_text() == 'first'
    assert not (results[0] / 'private').exists()
    assert (results[1] / 'private').read_text() == 'second'
    assert not (results[1] / 'public').exists()


@pytest.mark.parametrize('patterns', [[], ['*', '!link/**', '!file-link', '!broken']])
def test_negations_cannot_reinclude_symlinks(tmp_path: Path, patterns: List[str]) -> None:
    source, outside = tmp_path / 'source', tmp_path / 'outside'
    source.mkdir()
    outside.mkdir()
    (outside / 'secret').write_bytes(b'host secret')
    try:
        (source / 'link').symlink_to(outside, target_is_directory=True)
        (source / 'file-link').symlink_to(outside / 'secret')
        (source / 'broken').symlink_to(outside / 'missing')
    except OSError:
        pytest.skip('Symlinks require privileges on this system.')
    assert list(restore(snapshot(source, patterns)).walk()) == []


def test_gitignore_is_not_loaded_implicitly(tmp_path: Path) -> None:
    (tmp_path / '.gitignore').write_text('keep.py\n')
    (tmp_path / 'keep.py').write_text('data')
    saved = restore(WasmManager(tmp_path, exclude=['*.tmp']).read())
    assert (saved / 'keep.py').read_text() == 'data'
    assert (saved / '.gitignore').read_text() == 'keep.py\n'


@pytest.mark.parametrize('owner', ['manager', 'isolate'])
def test_invalid_pattern_fails_without_changing_files_and_allows_retry(tmp_path: Path, runtime: WasmRuntime, owner: str) -> None:
    (tmp_path / 'keep').write_bytes(b'keep')
    (tmp_path / 'private').write_bytes(b'private')
    manager = WasmManager(tmp_path, exclude=['\\'], runtime=runtime)
    instance = manager if owner == 'manager' else manager.get(snapshot(tmp_path))
    try:
        with pytest.raises(ValueError, match='Invalid git pattern'):
            instance.read()
        if isinstance(instance, WasmIsolate):
            instance.exclude = ('private',)
        else:
            instance.exclude = ['private']
        saved = restore(instance.read())
        assert (saved / 'keep').read_bytes() == b'keep'
        assert not (saved / 'private').exists()
        assert (tmp_path / 'private').read_bytes() == b'private'
    finally:
        if isinstance(instance, WasmIsolate):
            assert (instance.path / 'private').read_bytes() == b'private'
            instance.kill()
