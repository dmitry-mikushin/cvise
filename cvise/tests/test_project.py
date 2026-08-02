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
import subprocess
import sys
from pathlib import Path

import pytest

from cvise.utils.project import (
    ProjectError,
    baseline_build,
    configure,
    database_for,
    has_test,
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
        cmakelists = write_project(tmp_path / 'project')
        project = configure(cmakelists, tmp_path / 'build')
        staged = stage(project, tmp_path / 'staged')
        named = {e['file'] for e in json.loads(database_for(project, staged).read_text())}
        assert named, 'the database describes nothing'
        for source in project.sources:
            assert str(staged / source.relative_to(project.root)) in named

    def test_headers_are_named_too(self, tmp_path):
        """Most of a C++ program lives in them, and they have no command of their own."""
        cmakelists = write_project(tmp_path / 'project')
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
        cmakelists = write_project(tmp_path / 'project')
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
        cmakelists = write_project(tmp_path / 'project')
        project = configure(cmakelists, tmp_path / 'build')
        staged = stage(project, tmp_path / 'staged')
        original = tmp_path / 'build' / 'compile_commands.json'
        assert database_for(project, staged) != original
        assert all(
            Path(e['file']).suffix != '.hpp' for e in json.loads(original.read_text())
        )


class TestStagingAndPublish:
    def test_only_the_reducible_files_are_staged(self, tmp_path):
        cmakelists = write_project(tmp_path / 'project')
        project = configure(cmakelists, tmp_path / 'build')
        staged = stage(project, tmp_path / 'staged')
        assert (staged / 'src' / 'calc.cpp').is_file()
        assert not (staged / 'CMakeLists.txt').exists(), 'the build definition must not be reduced'

    def test_a_change_is_written_back(self, tmp_path):
        cmakelists = write_project(tmp_path / 'project')
        project = configure(cmakelists, tmp_path / 'build')
        staged = stage(project, tmp_path / 'staged')
        (staged / 'src' / 'calc.cpp').write_text('int calc() { return 1; }\n')
        assert publish(project, staged) == 1
        assert (project.root / 'src' / 'calc.cpp').read_text() == 'int calc() { return 1; }\n'

    def test_an_untouched_file_keeps_its_timestamp(self, tmp_path):
        """The user's build depends on it."""
        cmakelists = write_project(tmp_path / 'project')
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
        cmakelists = write_project(tmp_path / 'project')
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

