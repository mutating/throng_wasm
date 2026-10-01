![logo](https://raw.githubusercontent.com/mutating/throng_wasm/develop/docs/assets/logo_1.svg)

A [throng](https://github.com/mutating/throng) plugin for running **CPython
`wasm32-wasi` / `wasm32-wasip1`** commands in Wasmtime. Mypy and Pyflakes run
inside the sandbox, including their parsing and analysis; there is no native
fallback.

```python
from throng import throng

with throng('my_project')['wasm'].scope as isolate:
    isolate.install('mypy==1.14.1', 'pyflakes==3.3.2')
    print(isolate.run('ls').stdout)
    result = isolate.run('mypy --no-incremental --no-site-packages .')
    print(result.returncode, result.stdout, result.stderr)
    print(isolate.run('pyflakes .').stdout)
```

No environment variables, manual runtime installation or runtime downloads are
required. The CPython WASI archive is included in the Python package under
`throng_wasm/data/` and ships in both wheel and source distributions.
The first command verifies its SHA-256 and unpacks it **into memory**.
Project files, installed packages, guest temporary files and command output also
stay in memory. The plugin creates no temporary host files, directories or disk
caches. No third-party packages are preinstalled. Use `isolate.install(...)` to
add dependencies to that isolate. `ls` also runs inside WASM.

## Installation and runtime

Requires throng 0.0.4 or newer. Host Python: 3.8 or newer, on a platform supported by the Wasmtime Python wheel.
Install this repository with `python -m pip install -e .` or `uv pip install -e .`.
The `throng` entry point discovers `wasm` automatically. The Wasmtime
Python library is installed as a dependency; the Wasmtime CLI is not required.
The dependency range is bounded to the tested API family (25–38); Python 3.8
resolves an older compatible release.

The plugin prepares its bundled runtime automatically on the first command.
The archive comes from the CPython maintainer's GitHub release and is included
unchanged in this repository and the installed package. It occupies approximately
13.1 MiB compressed; the unpacked files contain approximately 37 MiB of data.
The [provenance and third-party licenses](throng_wasm/data/RUNTIME_LICENSES.txt)
ship beside the archive. A thread lock protects preparation; only a complete
runtime becomes available, shared within the host process.

Startup works offline, including in a fresh host process. A missing or corrupted
resource raises an explicit error; there is no network fallback. The optional
`THRONG_WASM_HOME` override still allows a user-supplied runtime. Installing new
dependencies with `install()` still accesses PyPI; their files then remain in
the isolate and its snapshots. `THRONG_WASM_CACHE` is unused. No runtime or JIT
cache is written to disk; the archive stays in its normal installed-package
location. Existing caches from older plugin versions are left untouched.

### Installing dependencies

Install dependencies with `isolate.install(*packages: str) -> None`:

```python
from throng import throng
from throng.errors import CannotInstallDependencyError

with throng('my_project')['wasm'].scope as isolate:
    isolate.install('mypy==1.14.1', 'pyflakes==3.3.2')
    print(isolate.run('mypy --no-site-packages .').stdout)
    print(isolate.run('pyflakes .').stdout)
    try:
        isolate.install('ruff')
    except CannotInstallDependencyError as error:
        print(error)  # identifies the requested package and the failure reason
    saved = isolate.read()
```

Installation accepts PyPI requirement strings, including version constraints,
extras and environment markers. The resolver uses the **guest's** CPython
version and WASI platform, even on host Python 3.8. It resolves transitive
dependencies with backtracking and checks wheel SHA-256 hashes against PyPI's
metadata. These hashes verify downloads; arbitrary package versions are not
pinned by the plugin. Pin your requirements for reproducibility.

Only compatible pure Python wheels are supported. Native binaries/extensions,
source builds, URL/VCS/local requirements and `.pth` files are rejected. Wheel
`purelib` files and `.data/scripts` Python scripts marked `#!python` or
`#!pythonw` are supported; other installation schemes and unmarked/shell scripts
are rejected. Script files are retained byte-for-byte in the isolate and can be
run explicitly with `isolate.run('python /scripts/NAME arguments')`. No shell,
executable lookup or automatic console-entry-point wrappers are added. This
uses the Python-script markers defined by the [wheel specification](https://packaging.python.org/en/latest/specifications/binary-distribution-format/#recommended-installer-features).

A pure Python package can still require OS
features unavailable in WASI at execution time. `install()` does not execute
package code or build scripts on the host, and does not install into the host's
Python environment. Download/resolution, incompatible packages, malformed wheels,
conflicts and filesystem failures raise `throng.errors.CannotInstallDependencyError`
with the requested packages and underlying cause.

A call installs all its requirements as one transaction. Existing explicitly
requested packages are retained; requesting the same name again replaces its
previous constraint. Dependencies are resolved together again, which can change
transitive versions. Repeating the same set of explicit requirements is a no-op
and works offline; `install()` with no arguments is also a no-op. There is no
uninstall API. New requirements need access to PyPI.

The isolate mutex covers interpreter inspection, downloads, resolution,
extraction and publication. Other `run()`, `read()` and `install()` calls wait;
they cannot see the old environment during installation or a partially updated
one. Failed preparation preserves the previous environment. Each invocation
already creates a fresh interpreter, so switching package files requires no
CPython rebuild or JIT recompilation. Other isolates keep their own dependencies.

Installed packages live in a separate memory tree mounted in WASI. Thus
`pyflakes .` checks the project without scanning installed linters. Downloads,
unpacked wheels and transaction staging stay in memory; publishing a prepared
environment swaps one tree reference under the mutex. `kill()`/scope exit
releases the isolate's tree. No host installation/build directory is created.
`read()` includes the packages, scripts and requirement manifest under the reserved
`.throng-wasm/` snapshot entry; `manager.get(saved)` restores them without a
network request. Do not use that reserved name for project files.

`cantok==0.0.43` and its transitive `dill==0.4.0` now install, including dill's
three scripts. The memory filesystem supplies a working virtual `/dev/null`,
but the guest still lacks `multiprocessing`, which dill imports.

### Memory-only storage

Wasmtime's [`Module(engine, bytes)`](https://github.com/bytecodealliance/wasmtime-py/blob/38.0.0/wasmtime/_module.py)
compiles in memory; `Module.from_file` itself reads bytes and calls that
constructor. Installing pure Python packages does not change this WASM module.
The Python bindings' standard WASI implementation exposes
[`WasiConfig.preopen_dir`](https://github.com/bytecodealliance/wasmtime-py/blob/38.0.0/wasmtime/_wasi.py)
for **host directories**, with no in-memory filesystem adapter. This plugin
instead implements the [WASI preview 1 calls](https://github.com/WebAssembly/WASI/blob/snapshot-01/phases/snapshot/docs.md)
used by CPython through Wasmtime host functions. Regular files, directories,
descriptors, seeks, renames, timestamps, directory iteration, standard streams
and `/dev/null` use private Python memory trees. Clocks, random bytes and
cancellable sleep are provided by host callbacks. Unsupported calls report a
WASI error. Symlinks, hard links and sockets are unsupported.

No host directory is preopened. Wasmtime's native WASI context is empty; only
its process-exit handler is used. Compiled code stays in memory and is reused
by the same `WasmRuntime`. `cache=True` is rejected to prevent disk caching.
This does not require a RAM disk or filesystem mount. The host Python interpreter
and installed Wasmtime library still come from the normal host environment;
ordinary Python import caching is outside the plugin's storage layer.

### Optional custom runtime

To use your own runtime instead of the packaged bundle, provide a directory
containing:

```text
runtime/
    python.wasm
    lib/
        python3.13/
            ... standard library from the same CPython build ...
```

The official CPython sources support WASI. Follow the [CPython build
instructions](https://devguide.python.org/getting-started/setup-building/#wasi)
to build it yourself, and assemble the directory above from `python.wasm`,
`Lib/` and the generated `_sysconfigdata_*.py`; include an empty `lib-dynload/`
directory. Use a release build for performance measurements.

The tested binary is CPython **3.13.11**, built from `python/cpython` with WASI
SDK 24 and distributed by CPython maintainer Brett Cannon. This is a build of
official CPython, **not a PSF-published binary release**. Its [build
workflow](https://github.com/brettcannon/cpython-wasi-build/blob/main/.github/workflows/release.yml)
and [release](https://github.com/brettcannon/cpython-wasi-build/releases/tag/v3.13.11)
are available upstream. The following **optional manual setup** reproduces the
custom runtime. It is not needed for normal or offline use. From the repository root (these commands explicitly create
the user-managed runtime inside ignored `venv/`):

```sh
mkdir -p venv/wasi
curl -fL https://github.com/brettcannon/cpython-wasi-build/releases/download/v3.13.11/python-3.13.11-wasi_sdk-24.zip -o venv/wasi/python.zip
python -c "import hashlib, pathlib; assert hashlib.sha256(pathlib.Path('venv/wasi/python.zip').read_bytes()).hexdigest() == 'e99a617738ade87cd263aa46cace7173faa91b5de994499c83e49d132c40bb77'"
python -m zipfile -e venv/wasi/python.zip venv/wasi/runtime

export THRONG_WASM_HOME="$PWD/venv/wasi/runtime"
```

Use `isolate.install(...)` for dependencies. Optionally, `THRONG_WASM_PACKAGES`
accepts existing package directories separated by the **host's**
`os.pathsep` (`:` on Unix, `;` on Windows). Dependencies must be compatible with
the **guest's** Python version. `install()` resolves for the guest even when the
host runs Python 3.8; Pyflakes 3.3.2 requires guest Python 3.9 or newer.
Native wheels and C extensions do not become WASI-compatible when
copied into this directory. Mypy 1.14.1 and Pyflakes 3.3.2 are verified; arbitrary
versions and plugins may depend on unavailable operating-system features.

A custom runtime can also be supplied directly:

```python
from throng_wasm import WasmManager, WasmRuntime

runtime = WasmRuntime(
    'venv/wasi/runtime',
    memory_limit=512 * 1024 * 1024,
)
manager = WasmManager('my_project', runtime=runtime)
print(manager.run('python -c "import sys; print(sys.platform)"').stdout)  # wasi
```

Creating/selecting a manager or isolate does not read the bundled archive or
compile CPython. Optional environment overrides are read on the first `get()`;
in-memory preparation and compilation happen on the first valid command (including the interpreter query performed by `install()`).
An explicit `THRONG_WASM_HOME` disables automatic preparation; in that case
use `isolate.install(...)` or provide `THRONG_WASM_PACKAGES` if linters are needed.
Custom runtime/package directories are read into memory once per runtime and
mounted read-only on every supported host Python version. They are never modified.
Reuse a manager/runtime to avoid loading and compiling the module again.

## Commands and lifecycle

Commands use POSIX-style argument quoting on every host. Supported forms are
`ls ...`, `python ...`, `python3 ...`, and the module aliases `mypy`, `pyflakes`,
`pycodestyle`, `flake8`. Install their packages before use. Mypy and Pyflakes
have integration tests; pycodestyle and flake8 have not been integration-tested. Console entry points from arbitrary packages
are not automatically turned into command aliases; use `python -m MODULE` or a
Python script. CPython handles its normal arguments (`-c`,
`-m`, scripts, `-O`, `--version`, etc.). There is no shell: pipes, redirection,
`&&`, variable expansion, and arbitrary native commands are not supported.
Unsupported executable names and invalid quoting raise `ValueError`.

`ls` is a small Python implementation executed inside the WASI guest, not the
host's executable. It supports paths (including quoted names), `-a`, `-A`, `-1`,
`-l`, `-d`, `--` and `--help`. Output uses one entry per line; `-l` uses numeric
ownership. Missing paths or unsupported options return code 2. It does not
provide the entire GNU/BSD `ls` option set.

A project snapshot becomes the guest root `/`, also its working directory.
Use project-relative paths; host absolute paths are not meaningful in the
sandbox. The standard library is mounted at `/python`, dependency directories
at `/packages/0`, `/packages/1`, etc. (isolate-installed packages come first),
wheel scripts at `/scripts`, and a fresh memory directory at `/tmp`.
`/python`, `/packages/*`, `/scripts`, `/tmp` and `/dev` are reserved guest mount names.

Every command gets a fresh interpreter, linear memory and in-memory `/tmp`.
Project file changes persist between commands **in the same isolate**, including
mypy's incremental cache. The original project is unchanged. `isolate.read()`
returns a ZIP snapshot of the current project files; `manager.get(state)`
restores exactly that snapshot. `manager.scope`, `manager.run()`, `chain()` and
cancellation tokens follow throng's API. `kill()` is idempotent; running or
reading after destruction raises `RuntimeError`. The internal `isolate.path`
is now a virtual path, not a host `pathlib.Path` or an `os.PathLike` object.

Initial snapshots include regular files and empty directories. Symlinks and
special files are skipped. By default, directory/file names `.git`, `.venv`,
`venv`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache` are excluded
at any depth. Exclusions can be passed through
the plugin slot or directly to `WasmManager`:

```python
manager = throng('my_project', exclude=['/private/', '*.tmp', '!results/keep.tmp'])['wasm']
```

`exclude=None` keeps the defaults above. An explicit list replaces the defaults;
`exclude=[]` includes all regular files and directories. To extend the defaults,
pass `[*DEFAULT_EXCLUDE, 'private/']` after importing `DEFAULT_EXCLUDE` from
`throng_wasm.state`.

Rules use dirstree's `gitwildmatch` syntax (via pathspec), in order; the last
matching rule wins. Paths are relative to the project root, use `/` separators,
and are matched case-sensitively regardless of the host filesystem:

| Rule | Effect |
| --- | --- |
| `secret` | Exclude that file or directory name at any depth. |
| `/secret` | Exclude only the name at the project root. |
| `build/` | Exclude directories named `build` and their contents, but not files named `build`. |
| `src/*.py` | Exclude Python files immediately inside `src`. |
| `src/**/*.py` | Exclude Python files at any depth inside `src`. |
| `!results/keep.tmp` | Include this path again after an earlier matching exclusion. |

`?` and character classes such as `[0-9]` are supported. Blank lines and lines
starting with `#` are ignored; escape a literal leading `#` or `!` with `\`.
`.gitignore` files are not loaded automatically. Paths are tested individually,
so a negated rule can include a child of an excluded directory; required parent
directories are recreated when restoring the snapshot.

Host files are enumerated with `dirstree.Crawler(only_files=False)`. Its `filter`
callback normalizes paths relative to the project, avoiding dependence on the
host's absolute path or current working directory. In-memory snapshots use the
same pathspec matcher without writing files to the host. Excluded file contents
are not read, although dirstree still traverses excluded directories.

Each isolate receives a copy of its manager's rules. `isolate.read()` applies
them again to project files, including files generated by commands. It does not
delete excluded files from the live isolate. A new `manager.get(state)` restores
the supplied snapshot exactly and uses that manager's rules for subsequent reads.
A directly constructed `WasmIsolate(state)` defaults to no exclusions; its
optional `exclude=` argument sets rules for subsequent reads. Installed packages,
their metadata and scripts always survive snapshots, even with `exclude=['*']`.
File ownership, permissions and timestamps are not restored.
Snapshot restoration rejects traversal, absolute paths, Windows device names,
components ending in a dot/space, and special members;
use trusted snapshots because archive size and decompression are not limited.

`WasmResult` exposes `success`, `returncode`, `stdout`, `stderr`, and
`killed_by_token`. Linter diagnostics retain the guest's exit code (normally
`1` for findings). Output is decoded as UTF-8 with replacement for invalid
bytes. A cancelled command that has not started has `returncode=None` and no
output; an interrupted command returns `130`. A WASM trap returns `1` with its
diagnostic in stderr. Configuration errors, invalid modules and missing runtime
files raise exceptions rather than masquerading as linter results.

### Running pytest

Pytest 9.1.1 runs inside the guest with these explicit WASI-compatible options:

```python
with throng('my_project')['wasm'].scope as isolate:
    isolate.install('pytest==9.1.1')
    result = isolate.run(
        'python -m pytest -q --capture=sys '
        '-p no:faulthandler -p no:cacheprovider '
        '--log-file=/tmp/pytest.log --basetemp=/tmp/pytest tests'
    )
    print(result.returncode, result.stdout, result.stderr)
```

An unmodified `python -m pytest` invocation fails in this runtime. The flags
avoid unsupported `os.dup()` calls in FD capture and faulthandler, disable the
cache plugin's `os.umask()` use, and bypass `os.getuid()` in automatic temporary-directory
setup. The explicit log file is optional; virtual `/dev/null` is available.
The guest's `/tmp` is private memory for each command and is released afterwards.

Integration tests exercise parametrization, fixtures, `tmp_path`, `capsys`,
`caplog`, `monkeypatch`, `pytest.raises`, skip/xfail and assertion failure reports.
Successful runs return 0 and failing tests return 1. FD capture (`capfd`),
pytest's cache features and tests requiring subprocesses or unsupported OS
features remain unavailable. Use `python -m pytest`; a bare `pytest` command
alias is not registered.

This guest pytest check uses a small test project. The plugin's own full suite
depends on host Wasmtime and concurrency facilities and currently stops during
collection inside WASI.

## Isolation and concurrency

Runtime libraries and externally supplied dependencies are mounted read-only on
all supported bindings, including Wasmtime 25 on host Python 3.8. Dependencies
installed with `install()` are private to the isolate and mounted writable.
Host environment variables, stdin, network sockets and other
host directories are not inherited. Standard WASI does not provide subprocess
creation, so guest pip, mypy daemon mode, `--install-types`, and tools that spawn host
executables are unsuitable here. Use the host-controlled `isolate.install(...)` before running a tool; mypy's
`--no-site-packages` makes dependency discovery explicit.

Calls sharing one runtime are serialized, including across its isolates, so
cancelling one command does not cancel another store. Separate `WasmRuntime`
objects allow concurrent execution. Tokens cover `run()` and `chain()` from
snapshot preparation through execution, including mutex waits. The plugin also
accepts an optional keyword-only `token` in `manager.read()`, `manager.get()`,
`isolate.read()`, `isolate.install()`, and the `WasmIsolate` constructor:

```python
from cantok import CancellationError, TimeoutToken

result = manager.run('python -c "while True: pass"', token=TimeoutToken(1))
assert result.killed_by_token

with manager.scope as isolate:
    try:
        isolate.install('mypy==1.14.1', token=TimeoutToken(30))
        saved = isolate.read(token=TimeoutToken(5))
    except CancellationError:
        # Cancelled installs keep the previous complete environment.
        pass
```

Command cancellation returns `WasmResult`: before execution, `returncode=None`;
after execution starts, `returncode=130`. `chain()` returns a result for every
command and skips remaining commands after cancellation. Non-command operations
raise the original cantok cancellation exception, including its specific type
and originating token. Package installation errors still raise
`CannotInstallDependencyError`. Unsuppressed token callback exceptions propagate
to the caller after stopping the guest; they never silently disable cancellation.
The first cancellation is latched for the entire operation or chain, including
with `ConditionToken(caching=False)`. A subsequent separate call uses a fresh
cancellation state. `CounterToken` counts token checks, not commands; the number
of checks depends on preparation work and scheduling.

Waiting operations and running WASM check tokens at approximately 10 ms intervals;
file processing also checks between files and chunks. Running WASM uses
[Wasmtime epoch interruption](https://docs.wasmtime.dev/examples-interrupting-wasm.html).
`kill()` interrupts a running operation before cleaning up. Cancellation releases
the isolate mutex only after guest execution has stopped; an interrupted command
may have already changed project files, which remain available through `read()`.

Wasmtime's synchronous compiler and an in-flight host network/filesystem call
cannot be forcibly stopped safely. Cancelled callers stop waiting for them. At
most one compilation continues per runtime and its result can be reused; it never
starts a cancelled command. A pending download only returns bytes and cannot
publish an environment after cancellation. All these tasks use memory, without
temporary directories or disk caches. This remains cooperative cancellation,
not a hard real-time deadline: Python scheduling, a native call holding the GIL,
and individual memory operations can delay observation. Token callbacks must be
quick and thread-safe: checks can run on the caller, preparation or cancellation
thread. An indefinitely blocking callback cannot itself be interrupted safely.

`memory_limit` bounds WASM linear memory. The memory filesystem separately uses
the same value as a per-command allocation budget for writable file contents and
output, plus limits on new files, path lengths and I/O vectors. This is not a
total host-memory limit: runtime images, snapshots, archive decompression and
Python object overhead are outside it. This is a WASI capability sandbox, not a
full Linux container.

## Ruff limitation

Ruff's Python package is a launcher for a **native Rust executable**, using
[`os.execvp`](https://github.com/astral-sh/ruff/blob/0.14.6/python/ruff/__main__.py).
It cannot run that binary inside CPython WASI. Ruff's published
[`ruff_wasm`](https://github.com/astral-sh/ruff/blob/main/crates/ruff_wasm/Cargo.toml)
uses `wasm-bindgen`/JavaScript imports and is not a standalone WASI command.
Supporting it would require a separate WASI port or a JavaScript environment;
this plugin does not claim Ruff support or run it outside the sandbox.
`ruff ...` reports this limitation explicitly. This does not establish that a
future Ruff WASI port is impossible.

## Tests and benchmarks

The full test suite runs with `python -m pytest`, including packaging checks and
real linter integrations, with no additional environment variables or build step.
The existing CI matrix runs the same suite on every configured OS and host Python.
Small WASM modules test Wasmtime, exit codes, traps, memory limits, cancellation
and fresh memory; integration tests use the bundled guest CPython 3.13.11.
Linters are installed through
`install()`, with no manually preinstalled packages. Tests also cover transactional
updates, dependency conflicts, rejected wheels, snapshot restoration, cleanup,
and simultaneous installation/execution/read/destruction. A lifecycle test forbids
host filesystem writes while creating an isolate, loading its runtime, installing
a package, running commands and restoring its snapshot. Filesystem tests cover
descriptor rights, read-only mounts, bounds, errors and cancellation during sleep.
They also exercise the first command with no runtime/package environment variables,
an empty memory cache and network access forbidden. Distribution tests verify
archive and license inclusion in wheel and sdist and launch the actual wheel
in a fresh process outside the checkout with network connections prohibited.
The packaging fixture builds fresh wheel and sdist artifacts in a temporary copy
of the package sources, independently for each pytest worker that needs them.
It leaves the checkout's build outputs untouched. Internet access is required
to obtain build dependencies and install the real linters from PyPI.

```sh
uv pip install -r requirements_dev.txt
uv pip install -e .
coverage erase
THRONG_COVERAGE_BRANCH=true coverage run -m pytest
coverage combine
coverage report -m --fail-under=100
ruff check throng_wasm tests
```

The existing CI also runs strict mypy checks. The target is 100% statement **and
branch** coverage of the plugin, including its reusable benchmark scenarios.
The guest's CPython/linter implementations are not included in that number.
The typing-only callback protocol signature has no implementation and is excluded
from runtime coverage.

Reusable scenarios live in `throng_wasm.benchmarks` and use
[microbenchmark](https://github.com/mutating/microbenchmark), following
[suby's benchmark organization](https://github.com/mutating/suby/blob/main/suby/benchmarks.py).
Preparation installs the linters and creates identical project corpora before any
timing starts. The context owns and cleans up the isolates and native fixture files:

```python
from throng_wasm import benchmarks

with benchmarks.prepare(number=10) as suite:
    result = suite.scenarios['mypy.100_files.wasm_reused'].run(warmup=2)
    print(result.mean)
    # suite.all is a microbenchmark.ScenarioGroup containing the prepared scenarios.
```

`tests/benchmarks/test_benchmarks.py` measures those same scenarios through
pytest-codspeed's `benchmark` fixture. It times one invocation, without nesting
`Scenario.run()`'s repetition loop. Preparation and warmup stay outside the timed
call; the cold-runtime scenario recompiles on every invocation. With ordinary
pytest, including `-n auto`, these tests execute the workloads without collecting
timings, and need no special flags or additional CI steps. To run just this folder:
`python -m pytest tests/benchmarks`.

The default suite has ten scenarios: native Python startup, three WASM startup
modes, and mypy (with and without incremental cache) and Pyflakes on 1 and 100
files. Passing `native_python=...` to `prepare()` adds the twelve native linter
comparisons, including pure-Python mypy and native-only Ruff.

Native and guest CPython versions and tool versions should match. The benchmark
installs its linters through `install()` before timing (installation/network time
is excluded), checks every invocation for success, reuses the compiled WASM module while
creating fresh stores, and records JIT compilation separately. It also
compares mypy against its interpreted native version to separate mypyc's benefit
from WASM overhead. Nothing in the benchmark reuses a Python interpreter across
commands. Results are specific to the measured machine and corpus.
