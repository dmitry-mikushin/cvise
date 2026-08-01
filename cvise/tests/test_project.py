"""The one thing the user names, and everything C-Vise derives from it.

This module is now the entire input side of the tool: name a CMakeLists.txt and
every later decision -- which files are reduced, which flags each is parsed with
-- follows from what CMake writes down. That makes its failure modes worth
stating explicitly, because each of them would otherwise surface much later, as
a reduction that quietly worked on the wrong set of files.
"""

import json
import shutil

import pytest

from cvise.utils.project import ProjectError, configure, sources_from

pytestmark = pytest.mark.skipif(shutil.which('cmake') is None, reason='requires cmake')


def write_project(root, sources, extra=''):
    root.mkdir(parents=True, exist_ok=True)
    for name, text in sources.items():
        (root / name).write_text(text)
    (root / 'CMakeLists.txt').write_text(
        'cmake_minimum_required(VERSION 3.20)\n'
        'project(demo C)\n'
        f'add_executable(prog {" ".join(sorted(sources))})\n' + extra
    )
    return root / 'CMakeLists.txt'


class TestConfigure:
    def test_returns_the_database_cmake_wrote(self, tmp_path):
        cmakelists = write_project(tmp_path / 'src', {'a.c': 'int main() { return 0; }\n'})
        project = configure(cmakelists, tmp_path / 'build')
        assert project.compilation_database.is_file()
        assert json.loads(project.compilation_database.read_text())

    def test_finds_the_translation_units(self, tmp_path):
        cmakelists = write_project(
            tmp_path / 'src',
            {'a.c': 'int a() { return 0; }\n', 'b.c': 'int main() { return 0; }\n'},
        )
        project = configure(cmakelists, tmp_path / 'build')
        assert sorted(p.name for p in project.sources) == ['a.c', 'b.c']

    def test_a_directory_is_taken_as_the_one_it_contains(self, tmp_path):
        """Naming the directory is naming its CMakeLists.txt; being pedantic here
        buys nothing and costs the user a retry."""
        root = tmp_path / 'src'
        write_project(root, {'a.c': 'int main() { return 0; }\n'})
        project = configure(root, tmp_path / 'build')
        assert project.cmakelists == (root / 'CMakeLists.txt').resolve()

    def test_a_path_that_is_not_a_cmakelists_is_refused(self, tmp_path):
        stray = tmp_path / 'notes.txt'
        stray.write_text('not a build system\n')
        with pytest.raises(ProjectError, match='CMakeLists'):
            configure(stray, tmp_path / 'build')

    def test_a_project_cmake_cannot_configure_is_refused_with_its_output(self, tmp_path):
        """The error the user needs is CMake's, not ours."""
        root = tmp_path / 'src'
        root.mkdir()
        (root / 'CMakeLists.txt').write_text('this is not valid cmake\n')
        with pytest.raises(ProjectError, match='compile_commands.json'):
            configure(root / 'CMakeLists.txt', tmp_path / 'build')


class TestSourcesFrom:
    def _database(self, tmp_path, entries):
        path = tmp_path / 'compile_commands.json'
        path.write_text(json.dumps(entries))
        return path

    def test_files_outside_the_project_are_not_reduced(self, tmp_path):
        """A vendored library or a system header is a dependency, not the subject.

        Deleting from one would change what the reduction means, and would not
        survive a rebuild of the dependency anyway.
        """
        root = tmp_path / 'project'
        root.mkdir()
        inside = root / 'mine.c'
        inside.write_text('int main() { return 0; }\n')
        outside = tmp_path / 'vendor.c'
        outside.write_text('int vendored() { return 0; }\n')
        db = self._database(
            tmp_path,
            [
                {'directory': str(tmp_path), 'file': str(inside), 'command': 'cc -c mine.c'},
                {'directory': str(tmp_path), 'file': str(outside), 'command': 'cc -c vendor.c'},
            ],
        )
        assert sources_from(db, root) == [inside]

    def test_relative_entries_are_resolved_against_their_directory(self, tmp_path):
        root = tmp_path / 'project'
        root.mkdir()
        source = root / 'mine.c'
        source.write_text('int main() { return 0; }\n')
        db = self._database(
            tmp_path, [{'directory': str(root), 'file': 'mine.c', 'command': 'cc -c mine.c'}]
        )
        assert sources_from(db, root) == [source]

    def test_a_file_listed_twice_is_reduced_once(self, tmp_path):
        """A file compiled into two targets appears twice; it is still one file."""
        root = tmp_path / 'project'
        root.mkdir()
        source = root / 'shared.c'
        source.write_text('int shared() { return 0; }\n')
        db = self._database(
            tmp_path,
            [
                {'directory': str(root), 'file': str(source), 'command': 'cc -c shared.c'},
                {'directory': str(root), 'file': str(source), 'command': 'cc -DX -c shared.c'},
            ],
        )
        assert sources_from(db, root) == [source]

    def test_an_empty_database_is_refused(self, tmp_path):
        """Reducing nothing at all would otherwise look like a successful run."""
        root = tmp_path / 'project'
        root.mkdir()
        db = self._database(tmp_path, [])
        with pytest.raises(ProjectError, match='nothing to reduce'):
            sources_from(db, root)

    def test_an_unreadable_database_is_refused(self, tmp_path):
        db = tmp_path / 'compile_commands.json'
        db.write_text('{ this is not json')
        with pytest.raises(ProjectError, match='cannot read'):
            sources_from(db, tmp_path)
