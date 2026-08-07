"""ClangPass on a directory, which is the only shape this fork ever gives it.

A project is reduced as one tree, so every clang_delta transformation now
arrives with a directory to walk and a compilation database to look flags up in.
Both of those were, until recently, wrong in ways that produced no error: the
database named files no pass ever works on, so clang_delta refused every one of
them, and the walk raised a counter that addressed nothing, so a reduction that
reached this pass did not finish.

Neither showed up as a failure. The exception a broken pass raises is caught and
logged by the job, and a pass that finds nothing is indistinguishable from a
pass with nothing to find -- so these tests drive the pass directly and insist
on a result, rather than watching a reduction and hoping.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from cvise.passes.abstract import PassResult
from cvise.passes.clang import ClangPass, DirState, sources_of
from cvise.utils import sigmonitor
from cvise.utils.externalprograms import find_external_programs
from cvise.utils.process import ProcessEventNotifier
from cvise.utils.project import configure, database_for, stage

pytestmark = pytest.mark.skipif(
    shutil.which('cmake') is None or shutil.which('ninja') is None,
    reason='requires cmake and ninja',
)


@pytest.fixture(autouse=True)
def signal_monitor():
    """ProcessEventNotifier expects it, and says so with a bare assertion."""
    sigmonitor.init()

TWO_FUNCTIONS = """#include "lib.hpp"
int used(int x) { return x + 1; }
int unused_one(int x) { return x + 2; }
int unused_two(int x) { return x + 3; }
"""


def a_project(tmp_path: Path):
    """A configured project, staged, with the database the passes are given."""
    root = tmp_path / 'project'
    root.mkdir(parents=True)
    (root / 'lib.hpp').write_text(
        '#pragma once\n'
        'int used(int x);\n'
        # Where the reduction of a C++ project actually has to happen.
        'inline int unused_inline(int x) { return x + 9; }\n'
    )
    (root / 'lib.cpp').write_text(TWO_FUNCTIONS)
    (root / 'main.cpp').write_text('#include "lib.hpp"\nint main() { return used(0); }\n')
    (root / 'CMakeLists.txt').write_text(
        'cmake_minimum_required(VERSION 3.20)\n'
        'project(demo CXX)\n'
        'add_executable(prog main.cpp lib.cpp)\n'
    )
    project = configure(root / 'CMakeLists.txt', tmp_path / 'build')
    staged = stage(project, tmp_path / 'staged')
    return project, staged, database_for(project, staged)


def a_pass(database: Path) -> ClangPass:
    programs = find_external_programs()
    # Asked as "is this a program I can run", not "does this path exist". The
    # name resolves to the bare string 'clang_delta' when the build is not
    # installed, and the repo root -- which is where pytest runs -- contains a
    # DIRECTORY of that name. `.exists()` therefore said yes, the skip did not
    # happen, and seven tests failed with "0 > 0" about a binary that was never
    # on PATH. shutil.which answers the question that was meant, and declines
    # directories.
    if not shutil.which(programs.get('clang_delta') or ''):
        pytest.skip('clang_delta is not built')
    return ClangPass(
        'remove-unused-function', external_programs=programs, compilation_database=str(database)
    )


class TestCounting:
    def test_a_staged_unit_offers_something(self, tmp_path):
        """Which is the whole test: it used to offer nothing, always.

        clang_delta refuses a file its database does not name. The database
        named the project's paths while every pass works on the staged copy, so
        the count was zero for every file of every project and the semantic
        passes were, in effect, not installed.
        """
        _, staged, database = a_project(tmp_path)
        assert a_pass(database).count_instances(staged / 'lib.cpp') > 0

    def test_a_unit_with_nothing_to_remove_offers_nothing(self, tmp_path):
        _, staged, database = a_project(tmp_path)
        assert a_pass(database).count_instances(staged / 'main.cpp') == 0


class TestWalkingTheDirectory:
    def test_the_first_unit_with_something_in_it_is_chosen(self, tmp_path):
        _, staged, database = a_project(tmp_path)
        state = a_pass(database).new(staged)
        assert isinstance(state, DirState)
        assert state.counter == 1
        assert state.instances > 0

    def test_the_position_runs_out(self, tmp_path):
        """It did not, and that is why a reduction reaching this pass did not end.

        Advancing past the last instance of the last unit has to yield nothing.
        The pass used to walk to the next unit inside the job and restart it at
        instance 1 -- invisible to the scheduler, which went on raising a
        counter that addressed nothing while the same transformation came back
        forever.
        """
        _, staged, database = a_project(tmp_path)
        pass_ = a_pass(database)
        state = pass_.new(staged)
        for _ in range(200):
            state = pass_.advance(staged, state)
            if state is None:
                return
        pytest.fail('the pass never ran out of positions')

    def test_every_instance_of_every_unit_is_visited(self, tmp_path):
        _, staged, database = a_project(tmp_path)
        pass_ = a_pass(database)
        seen = []
        state = pass_.new(staged)
        while state is not None and len(seen) < 200:
            seen.append((state.file_index, state.counter))
            state = pass_.advance(staged, state)
        for index, source in enumerate(sources_of(staged)):
            instances = pass_.count_instances(source)
            for counter in range(1, instances + 1):
                assert (index, counter) in seen, f'{source.name} instance {counter} was never offered'

    def test_a_header_is_offered_too(self, tmp_path):
        """Most of a C++ program is in them, and none of it was ever offered.

        A unit that includes a header and defines nothing of its own has
        nothing to remove -- which is the ordinary shape of modern C++. Leaving
        headers out of the walk meant the passes that delete functions,
        classes and templates had, for such a project, nothing to do at all.
        """
        _, staged, database = a_project(tmp_path)
        pass_ = a_pass(database)
        assert pass_.count_instances(staged / 'lib.hpp') > 0, 'nothing to remove in the header'
        visited = set()
        state = pass_.new(staged)
        while state is not None and len(visited) < 200:
            visited.add(sources_of(staged)[state.file_index].name)
            state = pass_.advance(staged, state)
        assert 'lib.hpp' in visited


class TestTransforming:
    def test_a_job_copy_is_transformed_with_the_flags_of_the_staged_file(self, tmp_path):
        """The job works on a copy, so the key is the staged path, not the copy.

        Getting this wrong is not a failure anyone sees: the pass raises, the
        job logs it and returns nothing, and the reduction goes on reporting
        that this transformation found nothing to do.
        """
        _, staged, database = a_project(tmp_path)
        pass_ = a_pass(database)
        state = pass_.new(staged)

        # Exactly what a job does: copy the test case, then transform the copy.
        job = tmp_path / 'job'
        job.mkdir()
        copy = job / staged.name
        shutil.copytree(staged, copy)
        before = (copy / 'lib.cpp').read_text()

        result, _ = pass_.transform(
            copy,
            state,
            ProcessEventNotifier(None),
            original_test_case=staged,
            written_paths=set(),
        )
        assert result == PassResult.OK, 'the transformation did not run'
        assert (copy / 'lib.cpp').read_text() != before, 'nothing was removed'

    def test_the_staged_tree_is_not_touched(self, tmp_path):
        _, staged, database = a_project(tmp_path)
        pass_ = a_pass(database)
        state = pass_.new(staged)
        job = tmp_path / 'job'
        job.mkdir()
        copy = job / staged.name
        shutil.copytree(staged, copy)
        before = (staged / 'lib.cpp').read_text()

        pass_.transform(copy, state, ProcessEventNotifier(None), original_test_case=staged,
                        written_paths=set())
        assert (staged / 'lib.cpp').read_text() == before


class TestTheResultCompiles:
    def test_what_the_pass_produced_is_still_a_translation_unit(self, tmp_path):
        """A pass that emits something the compiler rejects wastes a whole build."""
        project, staged, database = a_project(tmp_path)
        pass_ = a_pass(database)
        state = pass_.new(staged)
        job = tmp_path / 'job'
        job.mkdir()
        copy = job / staged.name
        shutil.copytree(staged, copy)
        pass_.transform(copy, state, ProcessEventNotifier(None), original_test_case=staged,
                        written_paths=set())

        command = project.check_command[str(project.root / 'lib.cpp')]
        argv = [a for a in command[:-1] if a != '-c'] + ['-fsyntax-only', str(copy / 'lib.cpp')]
        proc = subprocess.run(argv, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr[:400]
