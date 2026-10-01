from pathlib import Path

import pytest

from throng_wasm.commands import list_directory
from throng_wasm.runtime import command_arguments


@pytest.fixture
def listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / 'b.py').write_text('two')
    (tmp_path / 'a file.py').write_text('one')
    (tmp_path / '.hidden').write_text('hidden')
    (tmp_path / 'directory').mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.usefixtures('listing')
@pytest.mark.parametrize(('arguments', 'expected'), [
    ([], ['a file.py', 'b.py', 'directory']),
    (['-1'], ['a file.py', 'b.py', 'directory']),
    (['-a'], ['.', '..', '.hidden', 'a file.py', 'b.py', 'directory']),
    (['-A'], ['.hidden', 'a file.py', 'b.py', 'directory']),
    (['directory'], []),
    (['-d', 'directory'], ['directory']),
    (['a file.py'], ['a file.py']),
])
def test_listing(arguments: list, expected: list, capsys: pytest.CaptureFixture) -> None:
    assert list_directory(arguments) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == expected
    assert captured.err == ''


@pytest.mark.usefixtures('listing')
def test_long_listing(capsys: pytest.CaptureFixture) -> None:
    assert list_directory(['-la']) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 6
    assert any(line.startswith('-') and line.endswith(' a file.py') for line in lines)
    assert any(line.startswith('d') and line.endswith(' directory') for line in lines)


@pytest.mark.usefixtures('listing')
def test_missing_and_multiple(capsys: pytest.CaptureFixture) -> None:
    assert list_directory(['missing', 'directory', 'b.py']) == 2
    captured = capsys.readouterr()
    assert captured.out == 'directory:\nb.py\n'
    assert 'ls: missing:' in captured.err


@pytest.mark.parametrize('arguments', [['--invalid'], ['--help']])
def test_ls_parser(arguments: list, capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exception:
        list_directory(arguments)
    assert exception.value.code == (0 if arguments == ['--help'] else 2)
    output = capsys.readouterr()
    assert 'usage: ls' in output.out + output.err


@pytest.mark.usefixtures('listing')
def test_ls_end_of_options_and_directory_error(listing: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    (listing / '-file').write_bytes(b'')
    assert list_directory(['--', '-file']) == 0
    assert capsys.readouterr().out == '-file\n'
    original = Path.iterdir

    def denied(path):
        if path.name == 'directory':
            raise PermissionError(13, 'denied')
        return original(path)

    monkeypatch.setattr(Path, 'iterdir', denied)
    assert list_directory(['directory', 'b.py']) == 2
    output = capsys.readouterr()
    assert output.out.endswith('b.py\n')
    assert 'ls: directory: denied' in output.err


def test_missing_ls_resource_has_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('throng_wasm.runtime.get_data', lambda *_a: None)
    with pytest.raises(FileNotFoundError, match=r'commands\.py'):
        command_arguments('ls')
