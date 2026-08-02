"""C-Vise end to end, through the only interface it has.

Every test here drives the real command line the way a user does: a
CMakeLists.txt and an interestingness test, nothing else. The tests that used to
live here drove the interfaces this fork removed -- a bare file, a list of
files, a `-c` command string, hint application -- and they went with them,
because a test for a mode that no longer exists is worse than no test: it keeps
passing while describing a tool nobody can run.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

MAX_SHUTDOWN = 60  # seconds; generous, since normally shutdown is a fraction of one


@pytest.fixture
def subprocess_tmpdir() -> Iterator[Path]:
    """A private TMPDIR for the child, so leftovers are visible rather than lost in /tmp."""
    with tempfile.TemporaryDirectory(prefix='cvise-test-') as tmp_dir:
        yield Path(tmp_dir)


def write_project(root: Path, sources: dict[str, str], target: str = 'prog') -> Path:
    """A real CMake project, because that is the only thing C-Vise accepts."""
    root.mkdir(parents=True, exist_ok=True)
    for name, text in sources.items():
        (root / name).write_text(text)
    cmakelists = root / 'CMakeLists.txt'
    cmakelists.write_text(
        'cmake_minimum_required(VERSION 3.20)\n'
        'project(demo C)\n'
        f'add_executable({target} {" ".join(sorted(sources))})\n'
    )
    return cmakelists


def write_test(path: Path, body: str) -> Path:
    path.write_text('#!/bin/sh\n' + body)
    path.chmod(0o755)
    return path


def cvise_cli() -> Path:
    """The entry point to drive.

    Not the one in the source tree: that file still holds CMake placeholders and
    cannot find its own package, which is why this suite used to fail with
    "Cannot find cvise module directory" no matter what it was testing. The
    configured one lives in the build tree.
    """
    explicit = os.environ.get('CVISE_CLI')
    if explicit:
        return Path(explicit)
    for candidate in (Path.cwd() / 'cvise-cli.py', Path(__file__).parent.parent / 'build' / 'cvise-cli.py'):
        if candidate.is_file():
            return candidate
    pytest.skip('no configured cvise-cli.py found; build C-Vise or set CVISE_CLI')


def start_cvise(arguments: list[str], cwd: Path, subprocess_tmpdir: Path) -> subprocess.Popen:
    binary = cvise_cli()
    env = os.environ.copy()
    env['TMPDIR'] = str(subprocess_tmpdir)
    return subprocess.Popen(
        [sys.executable, str(binary)] + arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding='utf8',
        env=env,
        cwd=cwd,
    )


def run_cvise(arguments: list[str], cwd: Path, subprocess_tmpdir: Path) -> tuple[str, str]:
    proc = start_cvise(arguments, cwd, subprocess_tmpdir)
    stdout, stderr = proc.communicate(timeout=600)
    assert proc.returncode == 0, f'exit {proc.returncode}\nstderr:\n{stderr}\nstdout:\n{stdout}'
    return stdout, stderr


def assert_no_leftovers(subprocess_tmpdir: Path) -> None:
    leftovers = [p.name for p in subprocess_tmpdir.iterdir()]
    assert leftovers == [], f'the run left files behind in TMPDIR: {leftovers}'


needs_posix = pytest.mark.skipif(os.name != 'posix', reason='requires POSIX command-line tools')
needs_cmake = pytest.mark.skipif(shutil.which('cmake') is None, reason='requires cmake')
needs_cc = pytest.mark.skipif(shutil.which('gcc') is None, reason='requires gcc')


@needs_posix
@needs_cmake
@needs_cc
def test_reduces_a_project(tmp_path: Path, subprocess_tmpdir: Path):
    project = tmp_path / 'project'
    write_project(
        project,
        {
            'main.c': (
                'int keep_me() { return 42; }\n'
                'int drop_me() { return 1; }\n'
                'int main() { return keep_me(); }\n'
            ),
        },
    )
    # The one contract: the test refers to the project where the project is.
    # The overlay is what makes that correct -- every file this candidate
    # changed is served there instead of the original, and everything else is
    # read from the one shared tree.
    script = write_test(
        tmp_path / 'interesting.sh',
        f'cd {project} || exit 125\n'
        'gcc -c main.c -o /dev/null 2>/dev/null || exit 1\n'
        'grep -q keep_me main.c\n',
    )

    run_cvise([str(project / 'CMakeLists.txt'), str(script)], project, subprocess_tmpdir)

    result = (project / 'main.c').read_text()
    assert 'keep_me' in result, 'the property was destroyed'
    assert 'drop_me' not in result, 'nothing irrelevant was removed'
    assert_no_leftovers(subprocess_tmpdir)


@needs_posix
@needs_cmake
@needs_cc
def test_reduces_every_file_of_the_project(tmp_path: Path, subprocess_tmpdir: Path):
    """The project is the unit, so a file nobody needs is emptied like any other."""
    project = tmp_path / 'project'
    write_project(
        project,
        {
            'main.c': 'int main() { return 0; }\n',
            'other.c': 'void unused_here() {}\n',
        },
    )
    script = write_test(
        tmp_path / 'interesting.sh',
        f'cd {project} || exit 125\n'
        'gcc -Wall -Werror main.c other.c -o /dev/null 2>/dev/null\n',
    )

    run_cvise([str(project / 'CMakeLists.txt'), str(script)], project, subprocess_tmpdir)

    assert (project / 'main.c').read_text() == 'int main() {}\n'
    assert (project / 'other.c').read_text() == ''
    assert_no_leftovers(subprocess_tmpdir)


@needs_posix
@needs_cmake
@needs_cc
def test_honours_a_pass_group_file(tmp_path: Path, subprocess_tmpdir: Path):
    project = tmp_path / 'project'
    write_project(
        project,
        {
            'main.c': (
                'int bar() {\n  return 42;\n}\n'
                'int foo() {\n  return bar();\n}\n'
                'int main() {\n  return foo();\n}\n'
            )
        },
    )
    config = tmp_path / 'config.json'
    config.write_text(
        '{"interleaving": ['
        '{"pass": "lines", "arg": "0"},'
        '{"pass": "lines", "arg": "1"},'
        '{"pass": "lines", "arg": "2"}]}'
    )
    script = write_test(
        tmp_path / 'interesting.sh',
        f'cd {project} || exit 125\n'
        'gcc -c main.c -o /dev/null 2>/dev/null && grep -q foo main.c\n',
    )

    run_cvise(
        [str(project / 'CMakeLists.txt'), str(script), '--pass-group-file', str(config)],
        project,
        subprocess_tmpdir,
    )

    assert 'foo' in (project / 'main.c').read_text()
    assert_no_leftovers(subprocess_tmpdir)


@needs_posix
@needs_cmake
@pytest.mark.parametrize('signum', [signal.SIGINT, signal.SIGTERM], ids=['sigint', 'sigterm'])
def test_shuts_down_promptly_when_interrupted(tmp_path: Path, subprocess_tmpdir: Path, signum: int):
    """Control-C must not wait for jobs that are deliberately slow."""
    project = tmp_path / 'project'
    write_project(project, {'main.c': 'int main() { return 0; }\n'})
    flag = tmp_path / 'started'
    script = write_test(
        tmp_path / 'interesting.sh',
        f'touch {flag}\nsleep {MAX_SHUTDOWN * 2}\n',
    )

    proc = start_cvise(
        [str(project / 'CMakeLists.txt'), str(script), '--skip-interestingness-test-check', '-n', '5'],
        project,
        subprocess_tmpdir,
    )
    deadline = time.monotonic() + MAX_SHUTDOWN
    while not flag.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert flag.exists(), 'no job ever started, so this would prove nothing'

    proc.send_signal(signum)
    try:
        proc.communicate(timeout=MAX_SHUTDOWN)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    assert_no_leftovers(subprocess_tmpdir)


@needs_posix
@needs_cmake
def test_rejects_a_test_that_fails_on_the_untouched_project(tmp_path: Path, subprocess_tmpdir: Path):
    """If the pristine project is not interesting, every later verdict is meaningless."""
    project = tmp_path / 'project'
    write_project(project, {'main.c': 'int main() { return 0; }\n'})
    script = write_test(tmp_path / 'interesting.sh', 'exit 1\n')

    proc = start_cvise([str(project / 'CMakeLists.txt'), str(script)], project, subprocess_tmpdir)
    stdout, stderr = proc.communicate(timeout=600)
    assert proc.returncode != 0
    assert 'does not return' in (stdout + stderr), 'the refusal did not explain itself'


@needs_cmake
def test_rejects_a_path_that_is_not_a_cmakelists(tmp_path: Path, subprocess_tmpdir: Path):
    not_cmake = tmp_path / 'notes.txt'
    not_cmake.write_text('this is not a build system\n')
    script = write_test(tmp_path / 'interesting.sh', 'exit 0\n')

    proc = start_cvise([str(not_cmake), str(script)], tmp_path, subprocess_tmpdir)
    stdout, stderr = proc.communicate(timeout=120)
    assert proc.returncode != 0
    assert 'CMakeLists' in stdout + stderr


def test_lists_passes_without_a_project(tmp_path: Path, subprocess_tmpdir: Path):
    """--list-passes asks about C-Vise itself, so it needs no project."""
    stdout, _ = run_cvise(['--list-passes'], tmp_path, subprocess_tmpdir)
    assert 'ClangPass' in stdout
