"""What the project tells C-Vise, and the ways it used to tell it wrongly.

Every test here corresponds to a defect that produced a confident wrong answer
rather than a failure: a target that could not be found although the project
plainly builds it, headers that no semantic pass could touch, a deletion that
was undone on the way back to the user, a check that only compiles and so can
never notice a missing definition.
"""

import json
import os
import shutil
import re
import subprocess
import sys
from pathlib import Path

import pytest

from cvise.utils.project import (
    ProjectError,
    preset_definitions,
    baseline_build,
    build_for_test,
    targets_for_test,
    build_failure_report,
    configure,
    database_for,
    has_test,
    open_jobserver,
    tests_of as registered_tests,
    publish,
    sources_from,
    check_script,
    stage,
)

pytestmark = pytest.mark.skipif(
    shutil.which('cmake') is None or shutil.which('ninja') is None,
    reason='requires cmake and ninja',
)

CLANG_DELTA = '/usr/local/libexec/cvise/clang_delta'


def write_project(root, extra_targets=''):
    (root / 'src').mkdir(parents=True, exist_ok=True)
    (root / 'src' / 'calc.hpp').write_text(
        '#pragma once\nint calc();\ninline int unused_inline() { return 5; }\n'
    )
    (root / 'src' / 'calc.cpp').write_text('#include "calc.hpp"\nint calc() { return 42; }\n')
    (root / 'src' / 'main.cpp').write_text(
        '#include <cstdio>\n#include "calc.hpp"\nint main() { std::printf("V=%d\\n", calc()); }\n'
    )
    (root / 'CMakeLists.txt').write_text(
        'cmake_minimum_required(VERSION 3.20)\n'
        'project(demo CXX)\n'
        'add_executable(prog src/main.cpp src/calc.cpp)\n'
        'target_include_directories(prog PRIVATE src)\n' + extra_targets
    )
    return root / 'CMakeLists.txt'


TESTED_PROJECT = """
enable_testing()
add_test(NAME says_v COMMAND prog)
set_tests_properties(says_v PROPERTIES PASS_REGULAR_EXPRESSION "V=42")
"""


class TestWhichTestDecides:
    """A registered ctest test, not a build target.

    A target says only that something exited zero, and a test runner exits zero
    when the case it was asked for no longer exists -- so a criterion built on
    one is satisfied best by deleting the test. ctest can tell the difference,
    which is the whole reason it is what C-Vise asks for.
    """

    def test_a_registered_test_is_found(self, tmp_path):
        cmakelists = write_project(tmp_path / 'project', extra_targets=TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        assert has_test(project, 'says_v')

    def test_a_test_that_does_not_exist_is_not_found(self, tmp_path):
        cmakelists = write_project(tmp_path / 'project', extra_targets=TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        assert not has_test(project, 'no_such_test')

    def test_a_name_that_merely_contains_the_right_one_is_not_it(self, tmp_path):
        """The match is anchored, so `says` must not select `says_v`."""
        cmakelists = write_project(tmp_path / 'project', extra_targets=TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        assert not has_test(project, 'says')

    def test_a_project_with_no_tests_registers_none(self, tmp_path):
        """And the user is told so, rather than left to wonder about the name."""
        cmakelists = write_project(tmp_path / 'project')
        project = configure(cmakelists, tmp_path / 'build')
        assert not has_test(project, 'anything')
        assert registered_tests(project) == []

    def test_the_known_names_are_reported(self, tmp_path):
        cmakelists = write_project(tmp_path / 'project', extra_targets=TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        assert registered_tests(project) == ['says_v']


class TestTheCheckScript:
    @pytest.mark.skipif(not shutil.which('gcc'), reason='requires a C compiler')
    def test_it_builds_before_it_tests(self, tmp_path):
        """ctest does not build, so without this a candidate is judged by the
        binary the previous one left -- the exact wrong answer this program
        spends most of its care avoiding."""
        cmakelists = write_project(tmp_path / 'project', extra_targets=TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        script = check_script(project, 'says_v', tmp_path / 'check.sh')
        assert subprocess.run([str(script)], capture_output=True).returncode == 0

    @pytest.mark.skipif(not shutil.which('gcc'), reason='requires a C compiler')
    def test_a_test_that_is_not_there_is_a_failure(self, tmp_path):
        """`ctest -R nomatch` exits 0 on its own. MEASURED: it prints "No tests
        were found!!!" and succeeds, which would let a reduction satisfy the
        criterion by deleting the test. --no-tests=error is what makes it 8."""
        cmakelists = write_project(tmp_path / 'project', extra_targets=TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        script = check_script(project, 'gone_missing', tmp_path / 'check.sh')
        assert subprocess.run([str(script)], capture_output=True).returncode != 0

    @pytest.mark.skipif(not shutil.which('gcc'), reason='requires a C compiler')
    def test_the_output_is_kept(self, tmp_path):
        """A refusal that prints nothing is the worst thing this program can say."""
        cmakelists = write_project(tmp_path / 'project', extra_targets=TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        script = check_script(project, 'gone_missing', tmp_path / 'check.sh')
        proc = subprocess.run([str(script)], capture_output=True, text=True)
        assert proc.stdout.strip(), 'the check script said nothing about why it failed'


class TestDatabase:
    """What the semantic passes are told about the files they are given.

    clang_delta refuses to work on a file its database does not name, and it is
    right to. But a reduction never hands it the project's file -- it hands it
    the staged copy -- so a database written in the project's paths answers a
    question nobody asks: it looked correct, it was passed on every command
    line, and every semantic pass exited 255 on every file of every project.
    """

    def test_the_staged_files_are_the_ones_named(self, tmp_path):
        cmakelists = write_project(tmp_path / 'project', TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        staged = stage(project, tmp_path / 'staged')
        named = {e['file'] for e in json.loads(database_for(project, staged).read_text())}
        assert named, 'the database describes nothing'
        for source in project.sources:
            assert str(staged / source.relative_to(project.root)) in named

    def test_headers_are_named_too(self, tmp_path):
        """Most of a C++ program lives in them, and they have no command of their own."""
        cmakelists = write_project(tmp_path / 'project', TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        staged = stage(project, tmp_path / 'staged')
        named = {e['file'] for e in json.loads(database_for(project, staged).read_text())}
        headers = [s for s in project.sources if s.suffix == '.hpp']
        assert headers, 'the header was not even found'
        for header in headers:
            assert str(staged / header.relative_to(project.root)) in named

    @pytest.mark.skipif(not shutil.which(CLANG_DELTA), reason='clang_delta is not installed')
    @pytest.mark.parametrize('suffix', ['.hpp', '.cpp'])
    def test_clang_delta_accepts_a_staged_file(self, tmp_path, suffix):
        """The point of the entry, and it was wrong for sources as well as headers."""
        cmakelists = write_project(tmp_path / 'project', TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        staged = stage(project, tmp_path / 'staged')
        database = database_for(project, staged)
        source = next(s for s in project.sources if s.suffix == suffix)
        target = staged / source.relative_to(project.root)
        proc = subprocess.run(
            [
                CLANG_DELTA,
                '--query-instances=remove-unused-function',
                f'--compilation-database={database}',
                f'--compilation-database-key={target}',
                str(target),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr[:400]

    def test_the_original_database_is_left_alone(self, tmp_path):
        """CMake owns it and rewrites it on every reconfigure."""
        cmakelists = write_project(tmp_path / 'project', TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        staged = stage(project, tmp_path / 'staged')
        original = tmp_path / 'build' / 'compile_commands.json'
        assert database_for(project, staged) != original
        assert all(
            Path(e['file']).suffix != '.hpp' for e in json.loads(original.read_text())
        )


class TestStagingAndPublish:
    def test_only_the_reducible_files_are_staged(self, tmp_path):
        cmakelists = write_project(tmp_path / 'project', TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        staged = stage(project, tmp_path / 'staged')
        assert (staged / 'src' / 'calc.cpp').is_file()
        assert not (staged / 'CMakeLists.txt').exists(), 'the build definition must not be reduced'

    def test_a_change_is_written_back(self, tmp_path):
        cmakelists = write_project(tmp_path / 'project', TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        staged = stage(project, tmp_path / 'staged')
        (staged / 'src' / 'calc.cpp').write_text('int calc() { return 1; }\n')
        assert publish(project, staged) == 1
        assert (project.root / 'src' / 'calc.cpp').read_text() == 'int calc() { return 1; }\n'

    def test_an_untouched_file_keeps_its_timestamp(self, tmp_path):
        """The user's build depends on it."""
        cmakelists = write_project(tmp_path / 'project', TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        staged = stage(project, tmp_path / 'staged')
        before = (project.root / 'src' / 'main.cpp').stat().st_mtime_ns
        (staged / 'src' / 'calc.cpp').write_text('int calc() { return 1; }\n')
        publish(project, staged)
        assert (project.root / 'src' / 'main.cpp').stat().st_mtime_ns == before

    def test_a_deletion_is_written_back(self, tmp_path):
        """It used to be silently undone.

        publish skipped anything missing from the staged tree, so a file the
        reduction had proved unnecessary reappeared in the answer -- which then
        differed from the thing that was actually verified.
        """
        cmakelists = write_project(tmp_path / 'project', TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        staged = stage(project, tmp_path / 'staged')
        (staged / 'src' / 'calc.cpp').unlink()
        assert publish(project, staged) == 1
        assert not (project.root / 'src' / 'calc.cpp').exists()


class TestSources:
    def test_a_header_reached_through_a_dotdot_include_path_is_named_once(self, tmp_path):
        """One file, one spelling -- or the reduction refuses to start.

        The compiler echoes a dependency the way the include path spelled it,
        so a fixture reached through -Isrc/../fixtures comes back as
        ".../src/../fixtures/x.inc": absolute, and a different string from the
        ".../fixtures/x.inc" that the staged tree is built with.

        Two spellings of one file is not untidiness. The staged tree holds the
        normalised name, so the job's copy has nothing under the other one, and
        the overlay records that difference as a deletion -- whiting out a
        fixture nothing touched. MEASURED on ns-projection: C-Vise refused to
        start, saying the project was not interesting, on a project that builds.
        """
        root = tmp_path / 'project'
        (root / 'src').mkdir(parents=True)
        (root / 'fixtures').mkdir(parents=True)
        (root / 'fixtures' / 'table.inc').write_text('static const int kTable[] = {1, 2, 3};\n')
        (root / 'src' / 'main.cpp').write_text(
            '#include <cstdio>\n#include "table.inc"\nint main() { std::printf("%d\\n", kTable[0]); }\n'
        )
        (root / 'CMakeLists.txt').write_text(
            'cmake_minimum_required(VERSION 3.20)\n'
            'project(demo CXX)\n'
            'add_executable(prog src/main.cpp)\n'
            # The spelling that produced the defect: reached by going up and
            # back down, which is what a real project's layout tends to give.
            'target_include_directories(prog PRIVATE src/../fixtures)\n'
        )
        project = configure(root / 'CMakeLists.txt', tmp_path / 'build')

        included = [s for s in project.sources if s.name == 'table.inc']
        assert included, 'the include was not found at all'
        for path in included:
            assert '..' not in path.parts, f'{path} is not normalised'
            assert path == path.resolve()

        # And the staged tree must hold it under that same name, since that is
        # what the delta compares against.
        staged = stage(project, tmp_path / 'staged')
        for path in included:
            assert (staged / path.relative_to(project.root)).is_file()

    def test_generated_and_foreign_files_are_not_reduced(self, tmp_path):
        root = tmp_path / 'project'
        write_project(root)
        outside = tmp_path / 'vendor.cpp'
        outside.write_text('int vendored() { return 0; }\n')
        db = tmp_path / 'compile_commands.json'
        db.write_text(
            json.dumps(
                [
                    {'directory': str(root), 'file': str(root / 'src' / 'calc.cpp'), 'command': 'cc -c x'},
                    {'directory': str(root), 'file': str(outside), 'command': 'cc -c x'},
                ]
            )
        )
        assert sources_from(db, root) == [root / 'src' / 'calc.cpp']

    def test_an_empty_database_is_refused(self, tmp_path):
        db = tmp_path / 'compile_commands.json'
        db.write_text('[]')
        with pytest.raises(ProjectError, match='nothing to reduce'):
            sources_from(db, tmp_path)


CHECKED_PROJECT = '''cmake_minimum_required(VERSION 3.20)
project(demo C)
add_executable(prog main.c other.c)
add_custom_target(check
  COMMAND sh -c "$<TARGET_FILE:prog> | grep -q OTHER=1"
  VERBATIM)
add_dependencies(check prog)
'''
MAIN_C = '#include <stdio.h>\nint other(void);\nint main(void) { printf("OTHER=%d\\n", other()); return 0; }\n'
OTHER_C = 'int other(void) { return 1; }\n'


def write_checked_project(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'main.c').write_text(MAIN_C)
    (root / 'other.c').write_text(OTHER_C)
    (root / 'CMakeLists.txt').write_text(CHECKED_PROJECT)
    return root / 'CMakeLists.txt'


class TestTheBuildDirectory:
    """Whose answer is it?

    A reduction asks one question millions of times -- "is this candidate still
    interesting" -- and every job used to ask it in the same build directory.
    The overlay gave each job its own sources, so that much was honest; the
    objects, the link and ninja's record of what is up to date were shared, and
    those are what the answer is actually made of.

    The consequence is not slowness, it is a wrong answer in both directions. A
    file this candidate did not change is read from the pristine tree, with the
    pristine timestamp, which is older than the object the previous candidate
    left behind -- so ninja calls that object up to date and links it. The
    verdict then describes a program that no candidate ever was: an interesting
    one is discarded because it was graded on someone else's code, and a
    destroyed one is kept for the same reason.
    """

    @pytest.mark.skipif(not shutil.which('gcc'), reason='requires a C compiler')
    def test_a_job_is_graded_on_its_own_candidate(self, tmp_path):
        from cvise.utils import overlay

        library = overlay.library_path()
        if not library:
            pytest.skip('the overlay library is not built')

        cmakelists = write_checked_project(tmp_path / 'project')
        project = configure(cmakelists, tmp_path / 'build')
        baseline_build(project)

        def as_a_job(delta: Path) -> int:
            env = overlay.job_environment(
                dict(os.environ), delta, [project.root, project.build_dir]
            )
            return subprocess.run(
                ['cmake', '--build', str(project.build_dir), '--target', 'check'],
                capture_output=True,
                env=env,
            ).returncode

        # The first job's candidate breaks the property, so its build must fail.
        broken = tmp_path / 'delta-broken'
        place(broken, project.root / 'other.c', 'int other(void) { return 2; }\n')
        assert as_a_job(broken) != 0, 'a candidate that breaks the property looked interesting'

        # The second job changes something else and leaves other.c alone, so it
        # is read from the pristine tree -- with the pristine timestamp, which is
        # what made ninja keep the first job's object.
        intact = tmp_path / 'delta-intact'
        place(intact, project.root / 'main.c', MAIN_C.replace('return 0;', 'return 0; /* x */'))
        assert as_a_job(intact) == 0, 'a good candidate was graded on the previous one'

    @pytest.mark.skipif(not shutil.which('gcc'), reason='requires a C compiler')
    def test_the_shared_build_directory_keeps_the_pristine_answer(self, tmp_path):
        """Jobs write nothing into it, so the baseline stays the baseline."""
        from cvise.utils import overlay

        if not overlay.library_path():
            pytest.skip('the overlay library is not built')

        cmakelists = write_checked_project(tmp_path / 'project')
        project = configure(cmakelists, tmp_path / 'build')
        baseline_build(project)
        before = {
            p: p.stat().st_mtime_ns for p in project.build_dir.rglob('*') if p.is_file()
        }

        delta = tmp_path / 'delta'
        place(delta, project.root / 'other.c', 'int other(void) { return 2; }\n')
        subprocess.run(
            ['cmake', '--build', str(project.build_dir), '--target', 'check'],
            capture_output=True,
            env=overlay.job_environment(dict(os.environ), delta, [project.root, project.build_dir]),
        )

        after = {p: p.stat().st_mtime_ns for p in project.build_dir.rglob('*') if p.is_file()}
        assert after == before, 'a job wrote into the build directory every other job reads'

    @pytest.mark.skipif(not shutil.which('gcc'), reason='requires a C compiler')
    def test_a_deleted_source_is_not_linked_from_the_baseline(self, tmp_path):
        """The case worth being sure about, because the baseline still has its object.

        A reducer's most effective move is to remove a file, and the overlay
        records that as a whiteout: the file reads as absent for that job alone.
        But the baseline built an object from it and left it in the directory
        the job reads through. If ninja were to link that object, the verdict
        would be about code the candidate had deleted -- which is exactly the
        class of wrong answer the isolated build directory exists to prevent,
        reappearing through the very thing that makes it affordable.

        It does not: ninja refuses to build an edge whose input is gone. The
        candidate is rejected, which is the honest answer, since deleting a
        source from a CMake project also requires editing the CMakeLists.txt
        and that is not under reduction.
        """
        from cvise.utils import overlay

        if not overlay.library_path():
            pytest.skip('the overlay library is not built')

        cmakelists = write_checked_project(tmp_path / 'project')
        project = configure(cmakelists, tmp_path / 'build')
        baseline_build(project)
        assert list(project.build_dir.rglob('other.c.o')), 'the baseline never built the object'

        delta = tmp_path / 'delta'
        marker = delta / (str(project.root / 'other.c').lstrip('/') + overlay.WHITEOUT_SUFFIX)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        env = overlay.job_environment(dict(os.environ), delta, [project.root, project.build_dir])

        gone = subprocess.run(
            [sys.executable, '-c',
             f'import os; print(os.path.exists({str(project.root / "other.c")!r}))'],
            capture_output=True, text=True, env=env,
        ).stdout.strip()
        assert gone == 'False', 'the whiteout never reached the job'

        proc = subprocess.run(
            ['cmake', '--build', str(project.build_dir), '--target', 'check'],
            capture_output=True, env=env,
        )
        assert proc.returncode != 0, 'a file the candidate deleted was still linked in'
        assert (project.root / 'other.c').is_file(), 'the job deleted the shared source'


def place(delta: Path, original: Path, text: str) -> None:
    """Put one file in a job's delta, the way prepare_job_delta does."""
    target = delta / str(original).lstrip('/')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


class TestTheBaseline:
    """Built once so that no job has to build it again -- and no more than that."""

    @pytest.mark.skipif(not shutil.which('gcc'), reason='requires a C compiler')
    def test_the_project_is_compiled(self, tmp_path):
        cmakelists = write_checked_project(tmp_path / 'project')
        project = configure(cmakelists, tmp_path / 'build')
        baseline_build(project)
        assert list(project.build_dir.rglob('*.o')), 'nothing was compiled'

    @pytest.mark.skipif(not shutil.which('gcc'), reason='requires a C compiler')
    def test_the_users_check_is_not_run(self, tmp_path):
        """It is the reduction's question, not the baseline's.

        A check target usually runs the program, and running it here is neither
        cheaper than in a job nor of any use to one. Building it meant C-Vise
        started by executing the user's check -- and for a check that waits for
        something, that is a reducer which appears to hang before it has printed
        a line, and which cannot be interrupted tidily because it has not yet
        reached the code that cleans up after itself.
        """
        root = tmp_path / 'project'
        write_checked_project(root)
        witness = tmp_path / 'the-check-ran'
        (root / 'CMakeLists.txt').write_text(
            CHECKED_PROJECT.replace(
                'COMMAND sh -c "$<TARGET_FILE:prog> | grep -q OTHER=1"',
                f'COMMAND sh -c "touch {witness}"',
            )
        )
        project = configure(root / 'CMakeLists.txt', tmp_path / 'build')
        baseline_build(project)
        assert not witness.exists(), 'the baseline ran the interestingness test'



class TestTheTokenPool:
    """What bounds the machine is the number of compilers, not the -j of any one build.

    ninja is never given a -j. Both extremes were tried and both were wrong:
    with no bound at all the load reached 230 and the reduction's cgroup
    OOM-killed cc1plus; with -j 1 the machine sat half idle and candidates that
    touched many files timed out. Serialising the builds is wrong in the same
    way as -j 1 -- a candidate that changed one file compiles one object and
    holds the whole machine while everything waits for a build that cannot use
    it.

    A shared pool is adaptive where those are not: every build takes what it can
    use and leaves the rest.
    """

    def test_the_build_is_never_given_a_job_count(self, tmp_path):
        cmakelists = write_project(tmp_path / 'project', extra_targets=TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        text = check_script(project, 'says_v', tmp_path / 'check.sh').read_text()
        assert not re.search(r'cmake --build \S+ .*-j', text), text

    def test_the_builds_are_not_serialised(self, tmp_path):
        """One at a time is the other wrong answer, so nothing may lock here."""
        cmakelists = write_project(tmp_path / 'project', extra_targets=TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        text = check_script(project, 'says_v', tmp_path / 'check.sh').read_text()
        assert 'flock' not in text, text

    def test_the_pool_holds_the_tokens_it_was_asked_for(self, tmp_path):
        fd = open_jobserver(tmp_path / 'jobserver', 5)
        try:
            assert os.read(fd, 64) == b'x' * 5
        finally:
            os.close(fd)

    def test_a_pool_is_never_empty(self, tmp_path):
        """More jobs than cores subtracts to nothing, and no tokens is a deadlock."""
        fd = open_jobserver(tmp_path / 'jobserver', 0)
        try:
            assert os.read(fd, 64) == b'x'
        finally:
            os.close(fd)

    def test_the_pool_survives_having_no_readers(self, tmp_path):
        """Opened read-write on purpose: a write-only fifo would see EOF and the
        tokens would be gone the first time no build held it."""
        fd = open_jobserver(tmp_path / 'jobserver', 3)
        try:
            taken = os.read(fd, 1)
            os.write(fd, taken)
            assert os.read(fd, 64) == b'xxx'
        finally:
            os.close(fd)

    @pytest.mark.skipif(not shutil.which('gcc'), reason='requires a C compiler')
    def test_several_builds_share_one_pool(self, tmp_path, monkeypatch):
        """The property the whole thing exists for, checked against ninja itself.

        MEASURED on this machine: eight builds at once ran 192 compilers with no
        pool, 12 with a pool of 4 and 24 with a pool of 16 -- each ninja gets an
        implicit token of its own, so the total is builds plus tokens.
        """
        cmakelists = write_project(tmp_path / 'project', extra_targets=TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        fd = open_jobserver(tmp_path / 'jobserver', 2)
        try:
            env = {**os.environ,
                   'MAKEFLAGS': f'--jobserver-auth=fifo:{tmp_path / "jobserver"}'}
            proc = subprocess.run(['cmake', '--build', str(project.build_dir)],
                                  env=env, capture_output=True, text=True)
            assert proc.returncode == 0, proc.stderr[:400]
            assert 'Jobserver mode detected' in proc.stdout + proc.stderr, (
                'ninja ignored the pool: ' + (proc.stdout + proc.stderr)[:400]
            )
        finally:
            os.close(fd)


class TestTheBaselineIsTimed:
    """Because it is the only honest basis for a candidate's deadline.

    A candidate can never need more work than a build from nothing, and how
    long that is a property of the project rather than of C-Vise. MEASURED on
    ns-projection: the project builds from nothing in 145 s on 88 cores, which
    is 53 minutes for a job holding two of them -- against a fixed 300 s
    deadline, so every candidate from a pass that rewrites whole files timed
    out, always, and those passes were disabled for it.
    """

    @pytest.mark.skipif(not shutil.which('gcc'), reason='requires a C compiler')
    def test_it_says_how_long_it_took(self, tmp_path):
        cmakelists = write_project(tmp_path / 'project', TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        took = baseline_build(project)
        assert took > 0

    @pytest.mark.skipif(not shutil.which('gcc'), reason='requires a C compiler')
    def test_a_project_that_does_not_build_still_reports_a_time(self, tmp_path):
        """The warning is not a reason to leave the caller without a number."""
        root = tmp_path / 'project'
        write_project(root)
        (root / 'src' / 'calc.cpp').write_text('this is not C++\n')
        project = configure(root / 'CMakeLists.txt', tmp_path / 'build')
        assert baseline_build(project) > 0


class TestTheSharedTreeMovesWithTheReduction:
    """What a job reads through has to be the current best, not the original.

    A job's delta holds what differs between its candidate and the project. If
    the project only learns the answer when the run ends, then after the first
    accepted reduction that difference is the whole reduction rather than the
    candidate -- and every job copies all of it, with fresh timestamps, and
    rebuilds the project from scratch to judge one small change.

    MEASURED on ns-projection: 1246 of 1340 files in every delta, gigabytes of
    scratch, and a candidate that had to be valid in 1246 places at once.
    """

    def test_publishing_makes_the_next_delta_small_again(self, tmp_path):
        from cvise.utils import overlay

        root = tmp_path / 'project'
        write_project(root)
        project = configure(root / 'CMakeLists.txt', tmp_path / 'build')
        staged = stage(project, tmp_path / 'staged')

        # A reduction is accepted: the staged tree moves, the project does not.
        for path in sorted(staged.rglob('*')):
            if path.is_file():
                path.write_text(path.read_text().replace('\n', '\n'))
        (staged / 'src' / 'calc.cpp').write_text('int calc() { return 42; }\n')
        (staged / 'src' / 'main.cpp').write_text('int main() {}\n')

        job = tmp_path / 'before'
        job.mkdir()
        copy = job / staged.name
        shutil.copytree(staged, copy)
        _, changed_before = overlay.prepare_job_delta(
            job, [(project.root, copy)], project.sources
        )
        assert len(changed_before) >= 2, 'the reduction did not move at all'

        # Now the project is told, which is what this is about.
        publish(project, staged)

        job2 = tmp_path / 'after'
        job2.mkdir()
        copy2 = job2 / staged.name
        shutil.copytree(staged, copy2)
        _, unchanged_after = overlay.prepare_job_delta(
            job2, [(project.root, copy2)], project.sources
        )
        assert unchanged_after == [], (
            'a candidate that changed nothing still carried the whole reduction: '
            f'{[p.name for p in unchanged_after]}'
        )

        # And a candidate that does change something carries only that.
        (copy2 / 'src' / 'calc.cpp').write_text('int calc() { return 1; }\n')
        job3 = tmp_path / 'after2'
        job3.mkdir()
        shutil.copytree(copy2, job3 / staged.name)
        _, changed_after = overlay.prepare_job_delta(
            job3, [(project.root, job3 / staged.name)], project.sources
        )
        assert [p.name for p in changed_after] == ['calc.cpp']


class TestReadingABuildFailure:
    """A report that hid the first failure was read four times and believed.

    Every rebuild after a publish failed with `ar: <object>.o: No such file or
    directory`, and because the report was the last 2000 characters of ninja's
    output -- and one `ar` command for nine hundred objects is 25000 of them --
    that line was all there was to see. It was taken to mean the objects had
    vanished. Whether a compile had failed first could not be told from it at
    all, because any such block was thousands of characters earlier.
    """

    LONG_AR = '/usr/sbin/ar qc lib.a ' + ' '.join(f'CMakeFiles/x.dir/f{i}.cpp.o' for i in range(900))

    def two_failures(self):
        return (
            '[1/7] Building CXX object CMakeFiles/x.dir/a.cpp.o\n'
            'FAILED: [code=1] CMakeFiles/x.dir/a.cpp.o\n'
            '/usr/sbin/c++ -DFOO -c a.cpp -o a.cpp.o\n'
            'a.cpp:12:5: error: something went wrong here\n'
            'FAILED: [code=1] lib.a\n' + self.LONG_AR + '\n'
            '/usr/sbin/ar: CMakeFiles/x.dir/f5.cpp.o: No such file or directory\n'
            'ninja: build stopped: subcommand failed.\n'
        )

    def test_an_earlier_failure_is_not_hidden_by_a_later_one(self, tmp_path):
        """The regression itself: the tail showed only the last block."""
        report = build_failure_report(self.two_failures(), tmp_path)
        assert '2 build step(s) failed' in report
        assert 'a.cpp:12:5: error: something went wrong here' in report
        assert 'No such file or directory' in report

    def test_the_command_is_summarised_but_the_diagnostics_are_not(self, tmp_path):
        report = build_failure_report(self.two_failures(), tmp_path)
        assert self.LONG_AR not in report, 'the command body is the bulk and the least informative'
        assert '/usr/sbin/ar ...' in report, 'but which program ran still matters'
        assert f'({len(self.LONG_AR)} characters, elided)' in report
        assert len(report) < 2000, f'still {len(report)} characters, which nobody reads'

    def test_nothing_is_lost_by_summarising(self, tmp_path):
        build_failure_report(self.two_failures(), tmp_path)
        full = (tmp_path / 'cvise-last-build-failure.log').read_text()
        assert self.LONG_AR in full
        assert full == self.two_failures()

    def test_a_failure_that_is_not_an_edge_is_reported_whole(self, tmp_path):
        """ninja refusing to start says it in one line, and that line is all of it."""
        output = (
            "ninja: error: 'cpp/src/gone.cpp', needed by 'gone.cpp.o', "
            'missing and no known rule to make it\n'
        )
        report = build_failure_report(output, tmp_path)
        assert 'missing and no known rule to make it' in report

    def test_it_says_how_many_it_did_not_show(self, tmp_path):
        output = ''.join(
            f'FAILED: [code=1] out{i}.o\n/usr/bin/cc -c in{i}.c\nin{i}.c:1:1: error: no\n'
            for i in range(6)
        )
        report = build_failure_report(output, tmp_path)
        assert '6 build step(s) failed' in report
        assert 'and 3 more' in report

    def test_an_unwritable_build_directory_does_not_lose_the_report(self, tmp_path):
        """The diagnostic must survive the case where the diagnostic cannot be saved."""
        report = build_failure_report(self.two_failures(), tmp_path / 'does' / 'not' / 'exist')
        assert 'could not be written' in report
        assert 'a.cpp:12:5: error: something went wrong here' in report


class TestTheProjectsOwnConfigureSettings:
    """A project that ships CMakePresets.json has already said how to configure it.

    Some refuse anything else outright: one here answers a bare `cmake -S . -B`
    with "a raw invocation leaves CMAKE_PRESET_NAME unset and is refused".
    """

    def presets(self, tmp_path, body):
        (tmp_path / 'CMakePresets.json').write_text(json.dumps(body))
        return tmp_path

    def test_nothing_to_replay_without_the_file(self, tmp_path):
        assert preset_definitions(tmp_path) == []

    def test_the_default_preset_is_replayed(self, tmp_path):
        root = self.presets(tmp_path, {'configurePresets': [
            {'name': 'other', 'cacheVariables': {'A': 'no'}},
            {'name': 'default', 'cacheVariables': {'CMAKE_PRESET_NAME': 'default', 'B': 'yes'}},
        ]})
        assert set(preset_definitions(root)) == {'-DCMAKE_PRESET_NAME=default', '-DB=yes'}

    def test_a_lone_preset_is_replayed_whatever_it_is_called(self, tmp_path):
        root = self.presets(tmp_path, {'configurePresets': [
            {'name': 'only', 'cacheVariables': {'B': 'yes'}}]})
        assert preset_definitions(root) == ['-DB=yes']

    def test_several_with_no_default_is_left_to_cmake(self, tmp_path):
        """Which one the project meant is a question this cannot answer alone."""
        root = self.presets(tmp_path, {'configurePresets': [
            {'name': 'a', 'cacheVariables': {'A': '1'}},
            {'name': 'b', 'cacheVariables': {'B': '2'}}]})
        assert preset_definitions(root) == []

    def test_unreadable_presets_are_reported_not_ignored(self, tmp_path, caplog):
        (tmp_path / 'CMakePresets.json').write_text('{not json')
        with caplog.at_level('WARNING'):
            assert preset_definitions(tmp_path) == []
        assert caplog.records


class TestBuildingWhatTheTestNeeds:
    """The default target is the wrong answer for anything larger than a toy.

    MEASURED on ns-rtc: the default target is the whole tree, one component of
    it does not compile, and that component has nothing to do with the named
    test -- so the reduction refused to start over code the criterion never
    touches, while `--target ns_projection_unit_tests` builds in seconds.
    """

    def test_the_tests_own_executable_is_the_target(self, tmp_path):
        cmakelists = write_project(tmp_path / 'project', TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        baseline_build(project)
        targets = targets_for_test(project, 'says_v')
        assert targets, 'ctest knows the command of every test it registers'
        assert not Path(targets[0]).is_absolute(), 'a target is named inside the build directory'

    def test_it_builds_and_says_which_target(self, tmp_path):
        cmakelists = write_project(tmp_path / 'project', TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        test = 'says_v'
        took, target = build_for_test(project, test)
        assert took > 0
        assert has_test(project, test)
        assert target is not None, 'the target was worked out, not guessed at'

    def test_a_test_nobody_registers_falls_back_and_says_so(self, tmp_path, caplog):
        cmakelists = write_project(tmp_path / 'project', TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        with caplog.at_level('WARNING'):
            took, target = build_for_test(project, 'NoSuch.TestAtAll')
        assert target is None
        assert took > 0, 'the fallback still builds something'
        assert any('could not work out which target' in r.message for r in caplog.records)

    def test_the_named_target_is_what_cmake_is_asked_for(self, tmp_path, monkeypatch):
        cmakelists = write_project(tmp_path / 'project', TESTED_PROJECT)
        project = configure(cmakelists, tmp_path / 'build')
        seen = []

        real = subprocess.run

        def record(command, *args, **kwargs):
            if command[:2] == ['cmake', '--build']:
                seen.append(command)
            return real(command, *args, **kwargs)

        monkeypatch.setattr(subprocess, 'run', record)
        baseline_build(project, 'some_target')
        assert seen and seen[0][-2:] == ['--target', 'some_target']
