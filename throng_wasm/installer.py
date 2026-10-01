"""Resolve pure Python wheels in memory and publish an isolate-local environment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from email import message_from_bytes
from io import BytesIO
from pathlib import PurePosixPath
from typing import (
    IO,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Sequence,
    Tuple,
    Union,
    cast,
)
from urllib.request import urlopen
from zipfile import ZipFile

from cantok import DefaultToken
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import compatible_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version
from resolvelib import AbstractProvider, BaseReporter, Resolver
from resolvelib.resolvers import ResolutionImpossible, ResolutionTooDeep
from resolvelib.structs import RequirementInformation

from throng_wasm.cancellation import Cancellation
from throng_wasm.memory import MemoryPath
from throng_wasm.state import restore

ENVIRONMENT_DIRECTORY = '.throng-wasm'


def download(url: str) -> bytes:
    with cast(IO[bytes], urlopen(url, timeout=60)) as response:
        return response.read()


def requirement(text: str) -> Requirement:
    parsed = Requirement(text)
    if parsed.url is not None:
        raise ValueError(f'{text}: URL, local and VCS dependencies are unsupported; use a PyPI package name.')
    return parsed


def applies(marker: Union[Marker, None], environment: Dict[str, str], extras: Iterable[str] = ('',)) -> bool:
    return marker is None or any(marker.evaluate(dict(environment, extra=extra)) for extra in extras)


@dataclass(frozen=True)
class Wheel:
    name: str
    version: Version
    filename: str
    url: str
    sha256: str


@dataclass(frozen=True)
class Candidate:
    wheel: Wheel
    extras: Tuple[str, ...]


class WheelProvider(AbstractProvider[Requirement, Candidate, str]):
    def __init__(self, environment: Dict[str, str], cancellation: Union[Cancellation, None] = None) -> None:
        self.cancellation = cancellation if cancellation is not None else Cancellation(DefaultToken())
        self.environment = environment
        self.version = Version(environment['python_full_version'])
        self.tags = set(compatible_tags((self.version.major, self.version.minor), interpreter=f'cp{self.version.major}{self.version.minor}', platforms=['any']))
        self.index: Dict[str, List[Wheel]] = {}
        self.archives: Dict[Wheel, bytes] = {}

    def identify(self, requirement_or_candidate: Union[Requirement, Candidate]) -> str:
        if isinstance(requirement_or_candidate, Candidate):
            return requirement_or_candidate.wheel.name
        return canonicalize_name(requirement_or_candidate.name)

    def get_preference(
        self, identifier: str, resolutions: Mapping[str, Candidate],  # noqa: ARG002
        candidates: Mapping[str, Iterator[Candidate]],  # noqa: ARG002
        information: Mapping[str, Iterator[RequirementInformation[Requirement, Candidate]]],  # noqa: ARG002
        backtrack_causes: Sequence[RequirementInformation[Requirement, Candidate]],  # noqa: ARG002
    ) -> int:
        return 0

    def wheels(self, name: str) -> List[Wheel]:
        if name not in self.index:
            document = cast(Dict[str, Dict[str, List[Dict[str, object]]]], json.loads(self.cancellation.call(lambda: download(f'https://pypi.org/pypi/{name}/json'))))
            wheels: List[Wheel] = []
            for files in document['releases'].values():
                for file in files:
                    self.cancellation.check()
                    filename = str(file['filename'])
                    if not filename.endswith('.whl') or file['yanked']:
                        continue
                    wheel_name, version, _build, tags = parse_wheel_filename(filename)
                    if wheel_name != name or not self.tags.intersection(tags):
                        continue
                    if self.version not in SpecifierSet(str(file['requires_python'] or '')):
                        continue
                    digests = cast(Dict[str, str], file['digests'])
                    wheels.append(Wheel(name, version, filename, str(file['url']), digests['sha256']))
            by_version: Callable[[Wheel], Version] = lambda wheel: wheel.version
            self.index[name] = sorted(wheels, key=by_version, reverse=True)
        return self.index[name]

    def find_matches(
        self, identifier: str, requirements: Mapping[str, Iterator[Requirement]],
        incompatibilities: Mapping[str, Iterator[Candidate]],
    ) -> List[Candidate]:
        self.cancellation.check()
        constraints = list(requirements[identifier])
        rejected = set(incompatibilities[identifier])
        extras = tuple(sorted({canonicalize_name(extra) for constraint in constraints for extra in constraint.extras}))
        matches = []
        for wheel in self.wheels(identifier):
            candidate = Candidate(wheel, extras)
            if candidate not in rejected and all(self.is_satisfied_by(constraint, candidate) for constraint in constraints):
                matches.append(candidate)
        return matches

    def is_satisfied_by(self, requirement: Requirement, candidate: Candidate) -> bool:
        return (candidate.wheel.version in requirement.specifier
                and {canonicalize_name(extra) for extra in requirement.extras}.issubset(candidate.extras))

    def archive(self, wheel: Wheel) -> bytes:
        if wheel not in self.archives:
            data = self.cancellation.call(lambda: download(wheel.url))
            if hashlib.sha256(data).hexdigest() != wheel.sha256:
                raise ValueError(f'{wheel.filename}: SHA-256 mismatch.')
            self.archives[wheel] = data
        return self.archives[wheel]

    def get_dependencies(self, candidate: Candidate) -> List[Requirement]:
        wheel = candidate.wheel
        with ZipFile(BytesIO(self.archive(wheel))) as archive:
            metadata_files = [name for name in archive.namelist() if name.count('/') == 1 and name.endswith('.dist-info/METADATA')]
            if len(metadata_files) != 1:
                raise ValueError(f'{wheel.filename}: expected one .dist-info/METADATA file.')
            metadata = message_from_bytes(archive.read(metadata_files[0]))
            if canonicalize_name(str(metadata['Name'])) != wheel.name or Version(str(metadata['Version'])) != wheel.version:
                raise ValueError(f'{wheel.filename}: wheel name/version does not match its metadata.')
            if self.version not in SpecifierSet(str(metadata.get('Requires-Python', ''))):
                raise ValueError(f'{wheel.filename}: Requires-Python is incompatible with {self.version}.')
            provided = {canonicalize_name(str(extra)) for extra in metadata.get_all('Provides-Extra', [])}
            if not set(candidate.extras).issubset(provided):
                raise ValueError(f'{wheel.filename}: unknown extras {set(candidate.extras) - provided}.')
            dependencies = [requirement(str(text)) for text in metadata.get_all('Requires-Dist', [])]
            return [dependency for dependency in dependencies
                    if applies(dependency.marker, self.environment, ('', *candidate.extras))]


def unpack_wheel(data: bytes, filename: str, target: MemoryPath, *, check: Callable[[], None] = lambda: None) -> None:
    """Validate the archive and install purelib files and marked Python scripts."""
    extracted = restore(data, check=check)
    wheel_metadata = list(extracted.glob('*.dist-info/WHEEL'))
    if len(wheel_metadata) != 1 or message_from_bytes(wheel_metadata[0].read_bytes()).get('Root-Is-Purelib', '').lower() != 'true':
        raise ValueError(f'{filename}: only pure Python wheels are supported.')
    for path in list(extracted.walk()):
        check()
        if not path.is_file():
            continue
        relative = path.relative_to(extracted)
        scheme_target = target
        if relative.parts[0].endswith('.data'):
            if len(relative.parts) < 3 or relative.parts[1] not in {'purelib', 'scripts'}:
                raise ValueError(f'{filename}: unsupported wheel installation scheme: {relative}.')
            if relative.parts[1] == 'scripts':
                # Wheel's interpreter marker identifies Python scripts without
                # executing package code on the host. The guest runs these
                # explicitly with `python /scripts/NAME`, so keep bytes intact.
                shebang = path.read_bytes().split(b'\n', 1)[0].rstrip(b'\r')
                if len(relative.parts) != 3 or shebang not in {b'#!python', b'#!pythonw'}:
                    raise ValueError(f'{filename}: unsupported wheel script: {relative}; expected a Python script marked #!python or #!pythonw.')
                scheme_target = target.parent / 'scripts'
            relative = PurePosixPath(*relative.parts[2:])
        if path.suffix.lower() in {'.so', '.pyd', '.dll', '.dylib', '.exe', '.pth'}:
            raise ValueError(f'{filename}: unsupported native binary or .pth file: {relative}.')
        destination = scheme_target / relative
        if destination.exists():
            raise ValueError(f'{filename}: conflicting installed file: {relative}.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        path.replace(destination)


def install_packages(path: MemoryPath, packages: Sequence[str], environment: Dict[str, str], check_alive: Callable[[], None], *,
                     cancellation: Union[Cancellation, None] = None) -> None:
    """The caller holds the isolate mutex, including during resolution and commit."""
    cancellation = cancellation if cancellation is not None else Cancellation(DefaultToken())
    cancellation.check()
    destination = path / ENVIRONMENT_DIRECTORY
    manifest = destination / 'requirements.json'
    previous = cast(List[str], json.loads(manifest.read_text())) if manifest.exists() else []
    added = [requirement(package) for package in packages]
    added = [package for package in added if applies(package.marker, environment)]
    names = {canonicalize_name(package.name) for package in added}
    requested = [text for text in previous if canonicalize_name(requirement(text).name) not in names]
    requested.extend(str(package) for package in added)
    if sorted(requested) == sorted(previous):
        return
    provider = WheelProvider(environment, cancellation)
    resolver = Resolver(provider, BaseReporter[Requirement, Candidate, str]())
    try:
        result = resolver.resolve([requirement(text) for text in requested], max_rounds=1000)
    except ResolutionImpossible as exception:
        raise ValueError(
            f'No compatible pure Python wheels for CPython {provider.version} / WASI, or conflicting dependencies: {exception}. '
            'Native extensions and source builds are unsupported.',
        ) from exception
    except ResolutionTooDeep as exception:
        raise ValueError('Dependency resolution exceeded 1000 rounds; pin package versions to reduce the search.') from exception
    prepared = MemoryPath()
    target = prepared / 'packages'
    target.mkdir()
    for candidate in result.mapping.values():
        check_alive()
        unpack_wheel(provider.archive(candidate.wheel), candidate.wheel.filename, target, check=cancellation.check)
    (prepared / 'requirements.json').write_text(json.dumps(requested))
    check_alive()
    cancellation.check()
    path.publish(ENVIRONMENT_DIRECTORY, prepared)
