import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Event
from typing import Dict, List, Optional
from urllib.error import URLError
from zipfile import BadZipFile, ZipFile

import pytest
from cantok import CancellationError, ConditionToken, SimpleToken
from packaging.requirements import Requirement
from packaging.version import Version
from throng.errors import CannotInstallDependencyError

from tests.test_bootstrap import archive_bytes
from throng_wasm import (
    WasmIsolate,
    WasmManager,
    WasmResult,
    WasmRuntime,
    installer as module,
)
from throng_wasm.memory import MemoryPath

ENVIRONMENT = {
    'implementation_name': 'cpython', 'implementation_version': '3.13.11', 'os_name': 'posix',
    'platform_machine': 'wasm32', 'platform_release': '', 'platform_system': 'WASI', 'platform_version': '',
    'python_full_version': '3.13.11', 'platform_python_implementation': 'CPython',
    'python_version': '3.13', 'sys_platform': 'wasi',
}


class Index:
    def __init__(self) -> None:
        self.responses: Dict[str, bytes] = {}
        self.releases: dict = {}
        self.requests: List[str] = []

    def add(self, name: str, version: str = '1.0', *, requires: tuple = (), extras: tuple = (),  # noqa: PLR0913
            files: Optional[Dict[str, bytes]] = None, tag: str = 'py3-none-any',
            requires_python: str = '', yanked: bool = False, pure: str = 'true') -> str:
        filename = f'{name}-{version}-{tag}.whl'
        url = f'https://files.test/{filename}'
        dist = f'{name}-{version}.dist-info'
        metadata = f'Name: {name}\nVersion: {version}\nRequires-Python: {requires_python}\n'
        metadata += ''.join(f'Requires-Dist: {item}\n' for item in requires)
        metadata += ''.join(f'Provides-Extra: {item}\n' for item in extras)
        data = {
            f'{dist}/METADATA': metadata.encode(), f'{dist}/WHEEL': f'Root-Is-Purelib: {pure}\n'.encode(),
            f'{name}/__init__.py': f'VERSION = "{version}"\n'.encode(),
        }
        data.update(files or {})
        self.responses[url] = archive_bytes(data)
        entries = self.releases.setdefault(name, {}).setdefault(version, [])
        entries.append({'filename': filename, 'url': url, 'digests': {'sha256': hashlib.sha256(self.responses[url]).hexdigest()},
                        'requires_python': requires_python, 'yanked': yanked})
        self.responses[f'https://pypi.org/pypi/{name}/json'] = json.dumps({'releases': self.releases[name]}).encode()
        return url

    def download(self, url: str) -> bytes:
        self.requests.append(url)
        if url not in self.responses:
            raise URLError(f'not found: {url}')
        return self.responses[url]


@pytest.fixture
def index(monkeypatch: pytest.MonkeyPatch) -> Index:
    index = Index()
    monkeypatch.setattr(module, 'download', index.download)
    return index


@pytest.fixture
def isolate(tmp_path: Path, runtime: WasmRuntime, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runtime, '_execute', lambda *_args, **_kwargs: WasmResult(True, 0, json.dumps(ENVIRONMENT), ''))
    isolate = WasmManager(tmp_path, runtime=runtime).get(b'PK\x05\x06' + b'\0' * 18)
    yield isolate
    isolate.kill()


def test_download(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(url: str, timeout: int) -> BytesIO:
        assert url == 'https://example.test'
        assert timeout == 60
        return BytesIO(b'wheel')
    monkeypatch.setattr(module, 'urlopen', urlopen)
    assert module.download('https://example.test') == b'wheel'


def test_install_graph_extras_and_guest_markers(index: Index, isolate: WasmIsolate) -> None:
    index.add('tool', requires=('base>=1', 'extra; extra == "check"', 'wasi; sys_platform == "wasi"',
                                'wrong; sys_platform == "darwin"', 'old; python_version < "3.9"'), extras=('check',))
    index.add('base')
    index.add('extra')
    index.add('wasi')
    isolate.install('tool[check]')
    target = isolate._root / module.ENVIRONMENT_DIRECTORY / 'packages'
    assert {file.name for file in target.iterdir() if not file.name.endswith('.dist-info')} == {'tool', 'base', 'extra', 'wasi'}
    assert not list(isolate._root.glob('.throng-install-*'))
    assert not list(isolate._root.glob('.wheel-*'))
    assert json.loads((target.parent / 'requirements.json').read_text()) == ['tool[check]']
    requests = list(index.requests)
    isolate.install('tool[check]')
    isolate.install('ignored; python_version < "3.8"')
    isolate.install()
    assert index.requests == requests


def test_multiple_calls_update_and_snapshot(index: Index, isolate: WasmIsolate, tmp_path: Path, runtime: WasmRuntime) -> None:
    index.add('first')
    index.add('first', '2.0')
    index.add('second')
    isolate.install('first==1.0')
    isolate.install('second')
    isolate.install('first==2.0')
    state = isolate.read()
    clone = WasmManager(tmp_path, runtime=runtime).get(state)
    try:
        target = clone._root / module.ENVIRONMENT_DIRECTORY / 'packages'
        assert (target / 'first/__init__.py').read_text() == 'VERSION = "2.0"\n'
        assert not (target / 'first-1.0.dist-info').exists()
        assert (target / 'second').is_dir()
        clone.install('second')
    finally:
        clone.kill()
    isolate.kill()
    with pytest.raises(RuntimeError, match='destroyed'):
        isolate.install('first')


def test_backtracking_and_repeated_extra(index: Index, isolate: WasmIsolate) -> None:
    index.add('tool', '2.0', requires=('base==2.0',), extras=('x',))
    index.add('tool', '1.0', requires=('base==1.0', 'extra; extra == "x"'), extras=('x',))
    index.add('base')
    index.add('base', '2.0')
    index.add('extra')
    isolate.install('tool', 'base==1.0', 'tool[x]')
    assert (isolate._root / module.ENVIRONMENT_DIRECTORY / 'packages/tool-1.0.dist-info').is_dir()
    assert (isolate._root / module.ENVIRONMENT_DIRECTORY / 'packages/extra').is_dir()


@pytest.mark.parametrize('package', ['not found', '--index-url=https://evil.test', 'thing @ https://evil.test/wheel.whl', './local', 'missing'])
@pytest.mark.usefixtures('index')
def test_bad_requirements(isolate: WasmIsolate, package: str) -> None:
    with pytest.raises(CannotInstallDependencyError) as error:
        isolate.install(package)
    assert repr(package) in str(error.value)
    assert error.value.__cause__ is not None
    assert not (isolate._root / module.ENVIRONMENT_DIRECTORY).exists()


def test_incompatible_conflicting_and_missing(index: Index, isolate: WasmIsolate) -> None:
    index.add('native', tag='cp313-cp313-manylinux_2_17_x86_64')
    index.add('new', requires_python='>=3.14')
    index.add('yanked', yanked=True)
    index.add('conflict', requires=('base==1.0', 'base==2.0'))
    index.add('base')
    index.add('base', '2.0')
    for package in ['native', 'new', 'yanked', 'conflict']:
        with pytest.raises(CannotInstallDependencyError, match='No compatible pure Python wheels'):
            isolate.install(package)
    assert not (isolate._root / module.ENVIRONMENT_DIRECTORY).exists()


def test_index_filters(index: Index) -> None:
    url = index.add('tool')
    index.add('tool', '2.0', yanked=True)
    index.add('tool', '3.0', requires_python='>=4')
    index.add('tool', '4.0', tag='cp313-cp313-macosx_11_0_arm64')
    entries = index.releases['tool']['1.0']
    entries.extend([dict(entries[0], filename='tool-1.0.tar.gz'), dict(entries[0], filename='other-1.0-py3-none-any.whl')])
    index.responses['https://pypi.org/pypi/tool/json'] = json.dumps({'releases': index.releases['tool']}).encode()
    provider = module.WheelProvider(ENVIRONMENT)
    assert len(provider.wheels('tool')) == 1
    assert provider.wheels('tool')[0].url == url
    assert len(index.requests) == 1
    wheel = provider.wheels('tool')[0]
    candidate = module.Candidate(wheel, ())
    assert provider.identify(candidate) == 'tool'
    assert not provider.is_satisfied_by(Requirement('tool[x]'), candidate)
    assert provider.find_matches('tool', {'tool': iter([Requirement('tool')])}, {'tool': iter([candidate])}) == []


def test_candidates_keep_version_order_merged_extras_and_rejections(index: Index) -> None:
    for version in ['1.0', '10.0', '2.0']:
        index.add('tool', version)
    provider = module.WheelProvider(ENVIRONMENT)
    constraints = [Requirement('tool[check_one]>=2'), Requirement('tool[check-two]<11')]
    matches = provider.find_matches('tool', {'tool': iter(constraints)}, {'tool': iter(())})
    assert [str(candidate.wheel.version) for candidate in matches] == ['10.0', '2.0']
    assert all(candidate.extras == ('check-one', 'check-two') for candidate in matches)
    accepted = provider.find_matches('tool', {'tool': iter(constraints)}, {'tool': iter([matches[0]])})
    assert accepted == [matches[1]]
    assert index.requests == ['https://pypi.org/pypi/tool/json']


@pytest.mark.parametrize(('files', 'message'), [
    ({'../escape': b'oops'}, 'Unsafe snapshot'),
    ({'tool/native.so': b'ELF'}, 'unsupported native'),
    ({'tool.pth': b'import evil'}, 'unsupported native'),
    ({'tool-1.0.data/scripts/thing': b'python'}, 'unsupported wheel script'),
    ({'tool-1.0.data/scripts/thing': b'#!/bin/sh\necho hello'}, 'unsupported wheel script'),
    ({'tool-1.0.data/scripts/thing': b'\x7fELF'}, 'unsupported wheel script'),
    ({'tool-1.0.data/scripts/thing': b'#!python-not-a-marker\n'}, 'unsupported wheel script'),
    ({'tool-1.0.data/scripts/nested/thing': b'#!python\n'}, 'unsupported wheel script'),
    ({'tool-1.0.data/scripts/thing.exe': b'#!python\n'}, 'unsupported native'),
    ({'tool-1.0.data/headers/thing.h': b'header'}, 'unsupported wheel installation'),
    ({'tool-1.0.data/file': b'bad'}, 'unsupported wheel installation'),
    ({'other-1.0.dist-info/WHEEL': b'Root-Is-Purelib: true'}, 'only pure Python'),
    ({'tool-1.0.dist-info/WHEEL': b'Root-Is-Purelib: false'}, 'only pure Python'),
    ({'tool-1.0.dist-info/METADATA': b'Name: other\nVersion: 1.0'}, 'does not match'),
    ({'tool-1.0.dist-info/METADATA': b'Name: tool\nVersion: 2.0'}, 'does not match'),
    ({'tool-1.0.dist-info/METADATA': b'Name: tool\nVersion: 1.0\nRequires-Python: >=4'}, 'Requires-Python'),
    ({'other-1.0.dist-info/METADATA': b'Name: other\nVersion: 1.0'}, 'expected one'),
])
def test_invalid_wheels_rollback(index: Index, isolate: WasmIsolate, files: Dict[str, bytes], message: str) -> None:
    index.add('working')
    isolate.install('working')
    before = isolate.read()
    index.add('tool', files=files)
    with pytest.raises(CannotInstallDependencyError, match=message):
        isolate.install('tool')
    assert isolate.read() == before


def test_hash_invalid_zip_unknown_extra_and_conflict(index: Index, isolate: WasmIsolate) -> None:
    url = index.add('tool')
    original = index.responses[url]
    index.responses[url] = b'changed'
    with pytest.raises(CannotInstallDependencyError, match='SHA-256'):
        isolate.install('tool')
    index.responses[url] = original
    with pytest.raises(CannotInstallDependencyError, match='unknown extras'):
        isolate.install('tool[unknown]')
    index.add('other', files={'tool/__init__.py': b'conflicting'})
    with pytest.raises(CannotInstallDependencyError, match='conflicting installed file'):
        isolate.install('tool', 'other')
    wheel = module.Wheel('tool', Version('1.0'), 'tool-1.0-py3-none-any.whl', url, hashlib.sha256(b'broken').hexdigest())
    index.responses[url] = b'broken'
    provider = module.WheelProvider(ENVIRONMENT)
    with pytest.raises(BadZipFile):
        provider.get_dependencies(module.Candidate(wheel, ()))


def test_purelib_scheme(index: Index, isolate: WasmIsolate) -> None:
    index.add('tool', files={'tool-1.0.data/purelib/extra.py': b'VALUE = 1'})
    isolate.install('tool')
    target = isolate._root / module.ENVIRONMENT_DIRECTORY / 'packages'
    assert (target / 'extra.py').read_bytes() == b'VALUE = 1'


@pytest.mark.parametrize('shebang', [b'#!python\n', b'#!python\r\n', b'#!pythonw\n'])
def test_scripts_are_copied_without_execution_and_preserved_in_snapshot(index: Index, isolate: WasmIsolate, runtime: WasmRuntime, shebang: bytes) -> None:
    # Installation must copy this code, never execute it on the host.
    source = shebang + b'raise RuntimeError("only run when requested")\n'
    index.add('tool', files={'tool-1.0.data/scripts/thing': source})
    isolate.install('tool')
    target = isolate._environment
    assert (target / 'scripts/thing').read_bytes() == source
    assert not (target / 'packages/thing').exists()
    assert not (isolate.path / 'scripts').exists()
    state = isolate.read()
    isolate.kill()
    assert not target.exists()
    clone = WasmIsolate(state, runtime)
    try:
        assert (clone._environment / 'scripts/thing').read_bytes() == source
        assert not list(clone._root.glob('.throng-install-*'))
    finally:
        clone.kill()


def test_conflicting_scripts_rollback(index: Index, isolate: WasmIsolate) -> None:
    index.add('first', files={'first-1.0.data/scripts/shared': b'#!python\nprint(1)\n'})
    isolate.install('first')
    before = isolate.read()
    index.add('second', files={'second-1.0.data/scripts/shared': b'#!python\nprint(2)\n'})
    with pytest.raises(CannotInstallDependencyError, match=r'second.*conflicting installed file: shared'):
        isolate.install('second')
    assert isolate.read() == before
    index.add('first', '2.0')
    isolate.install('first==2.0')
    assert not (isolate._environment / 'scripts').exists()


@pytest.mark.parametrize('previous', [False, True])
def test_commit_failure_rolls_back(index: Index, isolate: WasmIsolate, monkeypatch: pytest.MonkeyPatch, previous: bool) -> None:
    index.add('tool')
    if previous:
        index.add('working')
        isolate.install('working')
    before = isolate.read()
    def fail_commit(_self: MemoryPath, name: str, _prepared: MemoryPath) -> None:
        assert name == module.ENVIRONMENT_DIRECTORY
        raise OSError('cannot publish')

    monkeypatch.setattr(MemoryPath, 'publish', fail_commit)
    with pytest.raises(CannotInstallDependencyError, match='cannot publish'):
        isolate.install('tool')
    assert isolate.read() == before


def test_interpreter_failure(isolate: WasmIsolate, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(isolate, '_run', lambda _command, _cancellation: WasmResult(False, stderr='interpreter failed'))
    with pytest.raises(CannotInstallDependencyError, match='interpreter failed'):
        isolate.install('tool')
    monkeypatch.setattr(isolate, '_run', lambda _command, _cancellation: WasmResult(True, stdout=None))
    with pytest.raises(CannotInstallDependencyError, match='Cannot inspect'):
        isolate.install('tool')


@pytest.mark.parametrize('operation', ['run', 'read', 'install', 'kill'])
def test_install_blocks_other_operations(index: Index, isolate: WasmIsolate, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    index.add('tool')
    index.add('other')
    started, finish, waiting, downloaded = Event(), Event(), Event(), Event()
    download = module.download

    def wait_download(url: str) -> bytes:
        if url.endswith('/tool/json'):
            started.set()
            assert finish.wait(10)
            downloaded.set()
        return download(url)

    def second() -> None:
        waiting.set()
        if operation == 'run':
            isolate.run('python')
        elif operation == 'read':
            state = isolate.read()
            with ZipFile(BytesIO(state)) as archive:
                assert '.throng-wasm/packages/tool/__init__.py' in archive.namelist()
        elif operation == 'install':
            isolate.install('other')
        else:
            isolate.kill()

    monkeypatch.setattr(module, 'download', wait_download)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(isolate.install, 'tool')
        assert started.wait(5)
        later = executor.submit(second)
        assert waiting.wait(5)
        try:
            if operation == 'kill':
                later.result(timeout=1)
                with pytest.raises(CannotInstallDependencyError, match='destroyed'):
                    first.result(timeout=1)
            else:
                assert not later.done()
                finish.set()
                first.result(timeout=10)
                later.result(timeout=10)
        finally:
            finish.set()
            assert downloaded.wait(3)
    if operation == 'kill':
        assert not isolate.path.exists()



def test_resolution_depth_has_explanation(index: Index, isolate: WasmIsolate, monkeypatch: pytest.MonkeyPatch) -> None:
    index.add('tool')

    def too_deep(*_args, **_kwargs):
        raise module.ResolutionTooDeep(1000)

    monkeypatch.setattr(module.Resolver, 'resolve', too_deep)
    with pytest.raises(CannotInstallDependencyError, match=r'tool.*exceeded 1000 rounds'):
        isolate.install('tool')


@pytest.mark.parametrize('stage', ['index', 'wheel'])
@pytest.mark.parametrize('cancel_with_error', [False, True])
def test_cancel_download_preserves_environment(index: Index, isolate: WasmIsolate, monkeypatch: pytest.MonkeyPatch,
                                               stage: str, cancel_with_error: bool) -> None:
    index.add('working')
    index.add('tool')
    isolate.install('working')
    before = isolate.read()
    entered, release, finished = Event(), Event(), Event()
    cancel = SimpleToken()
    failure = LookupError('token callback failed')

    def condition():
        if cancel.is_cancelled():
            raise failure
        return False

    token = ConditionToken(condition, suppress_exceptions=False) if cancel_with_error else cancel
    download = module.download

    def blocked_download(url):
        blocked = url.endswith('/tool/json') if stage == 'index' else '/tool-' in url
        if blocked:
            entered.set()
            try:
                assert release.wait(5)
                return download(url)
            finally:
                finished.set()
        return download(url)

    monkeypatch.setattr(module, 'download', blocked_download)
    with ThreadPoolExecutor() as executor:
        pending = executor.submit(isolate.install, 'tool', token=token)
        try:
            assert entered.wait(3)
            cancel.cancel()
            with pytest.raises(LookupError if cancel_with_error else CancellationError):
                pending.result(timeout=1)
            # The mutex is released even while the old network read is blocked.
            assert isolate.read() == before
            assert isolate.run('python').success
        finally:
            release.set()
            assert finished.wait(3)
    assert isolate.read() == before
    isolate.install('tool')
    assert (isolate._environment / 'packages/tool').is_dir()


def test_cancel_before_install_commit(index: Index, isolate: WasmIsolate, monkeypatch: pytest.MonkeyPatch) -> None:
    index.add('working')
    index.add('tool')
    isolate.install('working')
    before = isolate.read()
    token = SimpleToken()
    unpack = module.unpack_wheel

    def cancel_after_unpack(*args, **kwargs):
        unpack(*args, **kwargs)
        token.cancel()

    monkeypatch.setattr(module, 'unpack_wheel', cancel_after_unpack)
    with pytest.raises(CancellationError):
        isolate.install('tool', token=token)
    assert isolate.read() == before


def test_unpack_without_token_preserves_empty_files(index: Index) -> None:
    url = index.add('tool', files={'tool/empty': b''})
    root = MemoryPath()
    target = root / 'packages'
    target.mkdir()
    module.unpack_wheel(index.responses[url], 'tool-1.0-py3-none-any.whl', target)
    assert (target / 'tool/empty').read_bytes() == b''
    assert (target / 'tool/__init__.py').read_text() == 'VERSION = "1.0"\n'


def test_upgrade_rebuilds_transitive_graph_but_preserves_explicit_roots(index: Index, isolate: WasmIsolate) -> None:
    index.add('tool', '1.0', requires=('obsolete', 'shared'))
    index.add('tool', '2.0', requires=('newdep',))
    for name in ['obsolete', 'shared', 'newdep']:
        index.add(name)
    isolate.install('tool==1.0', 'shared')
    isolate.install('tool==2.0')
    packages = isolate._environment / 'packages'
    assert not (packages / 'obsolete').exists()
    assert not (packages / 'obsolete-1.0.dist-info').exists()
    assert (packages / 'shared').is_dir()
    assert (packages / 'newdep').is_dir()
    assert json.loads((isolate._environment / 'requirements.json').read_text()) == ['shared', 'tool==2.0']


def test_cycles_namespace_packages_and_download_reuse(index: Index, isolate: WasmIsolate) -> None:
    index.add('first', requires=('second',), files={'space/first.py': b'FIRST = 1'})
    index.add('second', requires=('first',), files={'space/second.py': b'SECOND = 2'})
    isolate.install('first')
    assert (isolate._environment / 'packages/space/first.py').read_bytes() == b'FIRST = 1'
    assert (isolate._environment / 'packages/space/second.py').read_bytes() == b'SECOND = 2'
    assert len(index.requests) == len(set(index.requests)) == 4


def test_normalized_names_and_extras_replace_same_root(index: Index, isolate: WasmIsolate) -> None:
    index.add('my_tool', extras=('fast_check',), requires=('extra; extra == "fast-check"',))
    index.responses['https://pypi.org/pypi/my-tool/json'] = index.responses.pop('https://pypi.org/pypi/my_tool/json')
    index.add('extra')
    isolate.install('My.Tool[FAST_CHECK]==1.0')
    assert (isolate._environment / 'packages/extra').is_dir()
    isolate.install('my_tool==1.0')
    assert not (isolate._environment / 'packages/extra').exists()
    assert json.loads((isolate._environment / 'requirements.json').read_text()) == ['my_tool==1.0']


@pytest.mark.parametrize('failure', ['network', 'bad-json', 'bad-index', 'missing-metadata', 'empty-metadata'])
def test_failed_install_retains_working_environment_and_can_retry(index: Index, isolate: WasmIsolate, failure: str, monkeypatch: pytest.MonkeyPatch) -> None:
    index.add('working')
    isolate.install('working')
    before = isolate.read()
    url = index.add('tool')
    original = dict(index.responses)
    if failure == 'network':
        download = module.download
        def offline(address):
            if address == url:
                raise URLError('connection interrupted')
            return download(address)
        monkeypatch.setattr(module, 'download', offline)
    elif failure in ['bad-json', 'bad-index']:
        index.responses['https://pypi.org/pypi/tool/json'] = b'{' if failure == 'bad-json' else b'{}'
    else:
        with ZipFile(BytesIO(index.responses[url])) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        if failure == 'missing-metadata':
            del files['tool-1.0.dist-info/METADATA']
        else:
            files['tool-1.0.dist-info/METADATA'] = b''
        index.responses[url] = archive_bytes(files)
        entry = index.releases['tool']['1.0'][0]
        entry['digests']['sha256'] = hashlib.sha256(index.responses[url]).hexdigest()
        index.responses['https://pypi.org/pypi/tool/json'] = json.dumps({'releases': index.releases['tool']}).encode()
    with pytest.raises(CannotInstallDependencyError, match='tool') as caught:
        isolate.install('tool')
    assert caught.value.__cause__ is not None
    assert isolate.read() == before
    index.responses.update(original)
    monkeypatch.setattr(module, 'download', index.download)
    isolate.install('tool')
    assert (isolate._environment / 'packages/tool').is_dir()


def test_package_code_is_not_executed_by_host_and_clones_install_independently(index: Index, isolate: WasmIsolate, runtime: WasmRuntime) -> None:
    index.add('tool', files={'tool/__init__.py': b'raise RuntimeError("must only run in guest")'})
    index.add('tool', '2.0')
    isolate.install('tool==1.0')
    state = isolate.read()
    clone = WasmIsolate(state, runtime)
    try:
        clone.install('tool==2.0')
        assert (clone._environment / 'packages/tool/__init__.py').read_text() == 'VERSION = "2.0"\n'
        assert (isolate._environment / 'packages/tool/__init__.py').read_bytes().startswith(b'raise RuntimeError')
        isolate.kill()
        assert (clone._environment / 'packages/tool').is_dir()
    finally:
        clone.kill()
