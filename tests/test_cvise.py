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


def write_project(root: Path, sources: dict[str, str], check: str = '') -> Path:
    """A real CMake project, because that is the only thing C-Vise accepts.

    The property lives in the project too: `check` is appended to the
    CMakeLists, and the reduction is told the name of the target to build. The
    project therefore describes both how it is built and what makes a variant
    interesting, which is the whole point of the interface.
    """
    root.mkdir(parents=True, exist_ok=True)
    for name, text in sources.items():
        (root / name).write_text(text)
    cmakelists = root / 'CMakeLists.txt'
    cmakelists.write_text(
        'cmake_minimum_required(VERSION 3.20)\n'
        'project(demo C)\n'
        f'add_executable(prog {" ".join(sorted(sources))})\n' + check
    )
    return cmakelists


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
needs_ninja = pytest.mark.skipif(shutil.which('ninja') is None, reason='requires ninja')

# add_dependencies, not DEPENDS: DEPENDS on a custom target takes FILES, so a
# target written with it does not rebuild the executable and happily tests the
# one left over from the previous candidate -- which passes whatever the
# candidate did. The reduction then empties the program and calls it
# interesting, correctly, because that is what it was asked.
KEEP_CHECK = (
    'add_custom_target(keeps\n'
    '  COMMAND sh -c "$<TARGET_FILE:prog> | grep -q KEEP_ME"\n'
    '  VERBATIM)\n'
    'add_dependencies(keeps prog)\n'
)


@needs_posix
@needs_cmake
@needs_ninja
@needs_cc
def test_reduces_a_project(tmp_path: Path, subprocess_tmpdir: Path):
    project = tmp_path / 'project'
    write_project(
        project,
        {
            'main.c': (
                '#include <stdio.h>\n'
                'int keep_me(void) { return 1; }\n'
                'int drop_me(void) { return 2; }\n'
                'int main(void) { if (keep_me()) puts("KEEP_ME"); return 0; }\n'
            ),
        },
        check=KEEP_CHECK,
    )

    # A fixed job count, because this asserts what the reduction achieved and
    # the suite runs alongside other tests on the same machine.
    before = (project / 'main.c').read_text()
    run_cvise([str(project / 'CMakeLists.txt'), 'keeps', '-n', '4'], project, subprocess_tmpdir)

    result = (project / 'main.c').read_text()
    assert 'KEEP_ME' in result, 'the property was destroyed'
    assert len(result) < len(before), 'nothing at all was removed'
    assert 'drop_me' not in result, 'the code the property does not need survived'
    assert_no_leftovers(subprocess_tmpdir)


@needs_posix
@needs_cmake
@needs_ninja
@needs_cc
def test_reduces_every_file_of_the_project(tmp_path: Path, subprocess_tmpdir: Path):
    """The project is the unit, so a file nobody needs is emptied like any other."""
    project = tmp_path / 'project'
    write_project(
        project,
        {
            'main.c': '#include <stdio.h>\nint main(void) { puts("KEEP_ME"); return 0; }\n',
            'other.c': 'int unused_here(void) { return 7; }\n',
        },
        check=KEEP_CHECK,
    )

    run_cvise([str(project / 'CMakeLists.txt'), 'keeps', '-n', '4'], project, subprocess_tmpdir)

    assert 'KEEP_ME' in (project / 'main.c').read_text()
    assert (project / 'other.c').read_text().strip() == '', 'the file nobody needs survived'
    assert_no_leftovers(subprocess_tmpdir)


@needs_posix
@needs_cmake
@needs_ninja
@needs_cc
def test_an_executable_target_is_accepted(tmp_path: Path, subprocess_tmpdir: Path):
    """Naming the executable must work; it once did not.

    Targets were looked up in the list `cmake --build --target help` prints,
    which contains only the phony primary targets -- so every executable and
    library was missing from it and the tool refused to start on the name a
    user is most likely to type.
    """
    project = tmp_path / 'project'
    write_project(project, {'main.c': 'int main(void) { return 0; }\n'})

    run_cvise([str(project / 'CMakeLists.txt'), 'prog'], project, subprocess_tmpdir)
    assert_no_leftovers(subprocess_tmpdir)


@needs_posix
@needs_cmake
@needs_ninja
def test_a_target_that_does_not_exist_is_refused(tmp_path: Path, subprocess_tmpdir: Path):
    """A typo must not look like "your project is not interesting"."""
    project = tmp_path / 'project'
    write_project(project, {'main.c': 'int main(void) { return 0; }\n'})

    proc = start_cvise(
        [str(project / 'CMakeLists.txt'), 'no_such_target'], project, subprocess_tmpdir
    )
    stdout, stderr = proc.communicate(timeout=600)
    assert proc.returncode != 0
    assert 'no target' in (stdout + stderr)


@needs_posix
@needs_cmake
@needs_ninja
@needs_cc
@pytest.mark.parametrize('signum', [signal.SIGINT, signal.SIGTERM], ids=['sigint', 'sigterm'])
def test_shuts_down_promptly_when_interrupted(tmp_path: Path, subprocess_tmpdir: Path, signum: int):
    """Control-C must not wait for jobs that are deliberately slow."""
    project = tmp_path / 'project'
    flag = tmp_path / 'started'
    write_project(
        project,
        {'main.c': 'int main(void) { return 0; }\n'},
        check=(
            'add_custom_target(slow\n'
            f'  COMMAND sh -c "touch {flag}; sleep {MAX_SHUTDOWN * 2}"\n'
            '  VERBATIM)\n'
            'add_dependencies(slow prog)\n'
        ),
    )

    proc = start_cvise(
        [str(project / 'CMakeLists.txt'), 'slow', '--skip-interestingness-test-check', '-n', '5'],
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
@needs_ninja
def test_rejects_a_target_that_fails_on_the_untouched_project(
    tmp_path: Path, subprocess_tmpdir: Path
):
    """If the pristine project is not interesting, every later verdict is meaningless."""
    project = tmp_path / 'project'
    write_project(
        project,
        {'main.c': 'int main(void) { return 0; }\n'},
        check='add_custom_target(always_fails COMMAND sh -c "exit 1" VERBATIM)\n',
    )

    proc = start_cvise(
        [str(project / 'CMakeLists.txt'), 'always_fails'], project, subprocess_tmpdir
    )
    stdout, stderr = proc.communicate(timeout=600)
    assert proc.returncode != 0
    assert 'does not return' in (stdout + stderr), 'the refusal did not explain itself'


@needs_cmake
def test_rejects_a_path_that_is_not_a_cmakelists(tmp_path: Path, subprocess_tmpdir: Path):
    not_cmake = tmp_path / 'notes.txt'
    not_cmake.write_text('this is not a build system\n')

    proc = start_cvise([str(not_cmake), 'anything'], tmp_path, subprocess_tmpdir)
    stdout, stderr = proc.communicate(timeout=120)
    assert proc.returncode != 0
    assert 'CMakeLists' in stdout + stderr


def test_lists_passes_without_a_project(tmp_path: Path, subprocess_tmpdir: Path):
    """--list-passes asks about C-Vise itself, so it needs no project."""
    stdout, _ = run_cvise(['--list-passes'], tmp_path, subprocess_tmpdir)
    assert 'ClangPass' in stdout
