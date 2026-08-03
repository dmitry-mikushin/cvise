"""Which files the build refers to, and which it merely reads.

The pass that removes files removes whatever nobody has claimed. Getting the
claim wrong is expensive in one direction and destructive in the other: claim
too little and every round proposes deleting every source file, each refused by
ninja rather than by a judgement about the code; claim too much and a header
nobody includes any more can never be removed, which is one of the few deletions
that does work on a CMake project.

The line between them is not a guess. CMake records, for every file it
compiles, the object it produces -- and records nothing of the sort for a
header, because no part of the build names one.
"""

import json
import shutil
from pathlib import Path

import pytest

from cvise.passes.compilationdatabase import FILEREF, CompilationDatabasePass, database_file
from cvise.utils.project import configure, database_for, stage

pytestmark = pytest.mark.skipif(
    shutil.which('cmake') is None or shutil.which('ninja') is None,
    reason='requires cmake and ninja',
)


def a_project(tmp_path: Path):
    root = tmp_path / 'project'
    root.mkdir(parents=True)
    (root / 'lib.hpp').write_text('#pragma once\nint used(int x);\ninline int spare(int x) { return x; }\n')
    (root / 'lib.cpp').write_text('#include "lib.hpp"\nint used(int x) { return x + 1; }\n')
    (root / 'main.cpp').write_text('#include "lib.hpp"\nint main() { return used(0); }\n')
    (root / 'CMakeLists.txt').write_text(
        'cmake_minimum_required(VERSION 3.20)\n'
        'project(demo CXX)\n'
        'add_executable(prog main.cpp lib.cpp)\n'
    )
    project = configure(root / 'CMakeLists.txt', tmp_path / 'build')
    staged = stage(project, tmp_path / 'staged')
    return project, staged, database_for(project, staged)


def referenced(database: Path, test_case: Path) -> set[str]:
    bundle = CompilationDatabasePass(
        arg=None, external_programs={}, compilation_database=str(database)
    ).generate_hints(test_case)
    names = set()
    for hint in bundle.hints:
        assert bundle.vocabulary[hint.type] == FILEREF
        names.add(bundle.vocabulary[hint.extra].decode())
    return names


class TestWhatTheDatabaseRecords:
    def test_a_translation_unit_has_an_output(self, tmp_path):
        project, staged, database = a_project(tmp_path)
        entries = {e['file']: e for e in json.loads(database.read_text())}
        for name in ('lib.cpp', 'main.cpp'):
            assert entries[str(staged / name)].get('output'), f'{name} has no output'

    def test_a_header_has_none(self, tmp_path):
        """It is what tells the two apart, so it must not be invented for headers."""
        project, staged, database = a_project(tmp_path)
        entries = {e['file']: e for e in json.loads(database.read_text())}
        assert str(staged / 'lib.hpp') in entries, 'the header lost its compile command'
        assert not entries[str(staged / 'lib.hpp')].get('output')


class TestWhatIsClaimed:
    def test_every_unit_the_build_compiles(self, tmp_path):
        """Otherwise every round proposes deleting each of them, and ninja refuses.

        Not a judgement about the code: a unit is named in the CMakeLists.txt,
        which is not under reduction, so the manifest simply will not load
        without it. MEASURED before this pass existed: twelve units, 66
        proposals, 0 accepted.
        """
        _, staged, database = a_project(tmp_path)
        assert referenced(database, staged) == {'lib.cpp', 'main.cpp'}

    def test_and_no_header(self, tmp_path):
        """Deleting a header that nobody includes is a real reduction, and works."""
        _, staged, database = a_project(tmp_path)
        assert 'lib.hpp' not in referenced(database, staged)

    def test_a_run_without_a_database_claims_nothing(self, tmp_path):
        _, staged, _ = a_project(tmp_path)
        pass_ = CompilationDatabasePass(arg=None, external_programs={}, compilation_database=None)
        assert pass_.generate_hints(staged).hints == []

    def test_an_unreadable_database_claims_nothing(self, tmp_path):
        _, staged, _ = a_project(tmp_path)
        broken = tmp_path / 'compile_commands.json'
        broken.write_text('{ not json')
        pass_ = CompilationDatabasePass(
            arg=None, external_programs={}, compilation_database=str(broken)
        )
        assert pass_.generate_hints(staged).hints == []

    def test_a_file_outside_the_test_case_is_not_claimed(self, tmp_path):
        """It cannot be deleted from a tree that does not hold it."""
        _, staged, database = a_project(tmp_path)
        entries = json.loads(database.read_text())
        entries.append(
            {
                'directory': str(tmp_path),
                'file': str(tmp_path / 'vendored.cpp'),
                'command': 'c++ -c vendored.cpp',
                'output': str(tmp_path / 'vendored.o'),
            }
        )
        elsewhere = tmp_path / 'compile_commands.json'
        elsewhere.write_text(json.dumps(entries))
        assert 'vendored.cpp' not in referenced(elsewhere, staged)


class TestItProposesNothingItself:
    def test_the_hints_are_special(self, tmp_path):
        """So the pass costs one enumeration and not a single candidate.

        A "@" type is never applied to a test case; it exists to be read by
        another pass. If these stopped being special they would start being
        attempted as transformations, and a transformation that says "this file
        is referred to" cannot mean anything.
        """
        pass_ = CompilationDatabasePass(arg=None, external_programs={}, compilation_database=None)
        assert pass_.output_hint_types() == [FILEREF]
        assert all(t.startswith(b'@') for t in pass_.output_hint_types())


class TestEitherWayOfNamingTheDatabase:
    """--compilation-database means the file OR the build directory holding it.

    clang_delta says exactly that in its own help and accepts either, so the
    directory form is not a misuse -- it is what the harness that drives this
    passes, because that is what clang_delta wants. Reading the value as a file
    and nothing else made the directory form raise IsADirectoryError, log a line
    nobody was watching for, and report that the build refers to no files at
    all. MEASURED on one database: 1 hint given the file, 0 given the directory,
    and 0 means every round proposing to delete every translation unit.
    """

    def test_the_file_is_accepted(self, tmp_path):
        _, staged, database = a_project(tmp_path)
        assert referenced(database, staged)

    def test_the_build_directory_is_accepted_too(self, tmp_path):
        _, staged, database = a_project(tmp_path)
        assert referenced(database.parent, staged), (
            'the directory form claimed nothing, so every translation unit is '
            'proposed for deletion every round'
        )

    def test_both_answer_the_same(self, tmp_path):
        _, staged, database = a_project(tmp_path)
        assert referenced(database, staged) == referenced(database.parent, staged)

    def test_which_file_each_form_resolves_to(self, tmp_path):
        database = tmp_path / 'compile_commands.json'
        database.write_text('[]')
        assert database_file(str(database)) == database
        assert database_file(str(tmp_path)) == database

    def test_a_path_that_is_neither_is_left_to_fail_where_it_is_read(self, tmp_path):
        """Not turned into a directory guess: the caller named a file that is
        missing, and saying so about that name is the useful diagnostic."""
        missing = tmp_path / 'nowhere.json'
        assert database_file(str(missing)) == missing
