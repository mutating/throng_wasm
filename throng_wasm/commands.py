"""Small command helpers executed as Python source inside the WASI guest."""
# ruff: noqa: T201

import argparse
import os
import stat
import sys
import time
from pathlib import Path
from typing import List, Sequence, cast


def _entry(path: Path, name: str, long_format: bool) -> str:
    if not long_format:
        return name
    info = path.lstat()
    timestamp = time.strftime('%b %d %H:%M', time.localtime(info.st_mtime))
    return f'{stat.filemode(info.st_mode)} {info.st_nlink} {info.st_uid} {info.st_gid} {info.st_size:>8} {timestamp} {name}'


def list_directory(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog='ls', description='List sandbox files, one entry per line.')
    parser.add_argument('-a', '--all', action='store_true')
    parser.add_argument('-A', '--almost-all', action='store_true')
    parser.add_argument('-l', action='store_true', help='show permissions, numeric ownership, size and time')
    parser.add_argument('-1', action='store_true', help='one entry per line (the default)')
    parser.add_argument('-d', '--directory', action='store_true')
    default_paths: List[str] = ['.']
    parser.add_argument('paths', nargs='*', default=default_paths)
    options = parser.parse_args(list(arguments))
    paths = cast(List[str], options.paths)
    show_all = cast(bool, options.all)
    show_hidden = show_all or cast(bool, options.almost_all)
    directory = cast(bool, options.directory)
    long_format = cast(bool, options.l)
    status = 0
    for name in paths:
        path = Path(name)
        try:
            if path.is_dir() and not directory:
                if len(paths) > 1:
                    print(f'{name}:')
                entries = sorted(child.name for child in path.iterdir() if show_hidden or not child.name.startswith('.'))
                if show_all:
                    entries = ['.', '..', *entries]
                for entry in entries:
                    # Normalize dot entries without following symlinks.
                    child = Path(os.path.abspath(path / entry))  # noqa: PTH100
                    print(_entry(child, entry, long_format))
            else:
                path.lstat()
                print(_entry(path, name, long_format))
        except OSError as exception:
            print(f'ls: {name}: {exception.strerror}', file=sys.stderr)
            status = 2
    return status
