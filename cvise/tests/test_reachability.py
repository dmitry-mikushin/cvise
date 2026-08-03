"""Can the test reach this file at all?

The call graph is in the object files, not in the sources: what each object
defines and what each object needs. Following that from `main` gives what the
program can use; the rest contributes nothing to this test.

MEASURED on ns-projection, ten hours into a reduction: 557 objects, 106
reachable, 451 not -- while the passes that were running had emptied files one
at a time and reached 39.5% in ten hours.

These tests use real object files, built here, because the whole point is that
the answer comes from the linker's view of the program rather than from a model
of it. A mocked nm would test the model.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from cvise.passes.reachability import ReachabilityPass

pytestmark = pytest.mark.skipif(
    shutil.which('nm') is None or shutil.which('readelf') is None or shutil.which('c++') is None,
    reason='requires nm, readelf and a C++ compiler',
)


def build(tmp_path: Path, sources: dict[str, str]) -> tuple[Path, Path]:
    """A source tree and a compilation database naming real objects."""
    src = tmp_path / 'src'
    src.mkdir(parents=True, exist_ok=True)
    build_dir = tmp_path / 'build'
    (build_dir / 'cvise_compile_commands').mkdir(parents=True, exist_ok=True)

    entries = []
    for name, text in sources.items():
        (src / name).write_text(text)
        obj = build_dir / (name + '.o')
        subprocess.run(['c++', '-c', str(src / name), '-o', str(obj)], check=True)
        entries.append(
            {
                'directory': str(build_dir),
                'file': str(src / name),
                'command': f'c++ -c {src / name}',
                'output': str(obj),
            }
        )
    database = build_dir / 'cvise_compile_commands' / 'compile_commands.json'
    import json

    database.write_text(json.dumps(entries))
    return src, database


def proposed(src: Path, database: Path) -> set[str]:
    bundle = ReachabilityPass(
        arg=None, external_programs={'nm': shutil.which('nm'), 'readelf': shutil.which('readelf')}, compilation_database=str(database)
    ).generate_hints(src)
    return {bundle.vocabulary[p.path].decode() for h in bundle.hints for p in h.patches}


class TestWhatTheTestCanReach:
    def test_a_file_nothing_calls_is_proposed(self, tmp_path):
        src, db = build(
            tmp_path,
            {
                'main.cpp': 'int used(); int main() { return used(); }\n',
                'used.cpp': 'int used() { return 0; }\n',
                'orphan.cpp': 'int orphan() { return 7; }\n',
            },
        )
        assert proposed(src, db) == {'orphan.cpp'}

    def test_what_the_program_uses_is_left_alone(self, tmp_path):
        src, db = build(
            tmp_path,
            {
                'main.cpp': 'int used(); int main() { return used(); }\n',
                'used.cpp': 'int used() { return 0; }\n',
            },
        )
        assert proposed(src, db) == set()

    def test_reached_through_another_file_is_left_alone(self, tmp_path):
        """Transitively, or the pass would empty the middle of the program."""
        src, db = build(
            tmp_path,
            {
                'main.cpp': 'int middle(); int main() { return middle(); }\n',
                'middle.cpp': 'int deep(); int middle() { return deep(); }\n',
                'deep.cpp': 'int deep() { return 1; }\n',
            },
        )
        assert proposed(src, db) == set()

    def test_reached_only_through_a_pointer_is_left_alone(self, tmp_path):
        """The case that makes a source-level analysis wrong, and this one right.

        Nothing calls target() by name; its address is taken and called through
        a pointer. That is a relocation like any other, so the object stays
        reachable without anything having to reason about it.
        """
        src, db = build(
            tmp_path,
            {
                'main.cpp': (
                    'int target();\n'
                    'int main() { int (*f)() = &target; return f(); }\n'
                ),
                'target.cpp': 'int target() { return 3; }\n',
            },
        )
        assert proposed(src, db) == set()

    def test_an_already_empty_file_is_not_proposed(self, tmp_path):
        """There is nothing to remove, and a candidate that changes nothing is a bug."""
        src, db = build(
            tmp_path,
            {
                'main.cpp': 'int main() { return 0; }\n',
                'orphan.cpp': '\n',
            },
        )
        (src / 'orphan.cpp').write_text('')
        assert proposed(src, db) == set()

    def test_the_patch_covers_the_whole_file(self, tmp_path):
        src, db = build(
            tmp_path,
            {
                'main.cpp': 'int main() { return 0; }\n',
                'orphan.cpp': 'int orphan() { return 7; }\n',
            },
        )
        bundle = ReachabilityPass(
            arg=None,
            external_programs={'nm': shutil.which('nm'), 'readelf': shutil.which('readelf')},
            compilation_database=str(db),
        ).generate_hints(src)
        patch = bundle.hints[0].patches[0]
        assert patch.left == 0
        assert patch.right == (src / 'orphan.cpp').stat().st_size

    def test_without_a_main_nothing_is_proposed(self, tmp_path):
        """No entry point means no reachability, and guessing one would be worse."""
        src, db = build(tmp_path, {'a.cpp': 'int a() { return 1; }\n'})
        assert proposed(src, db) == set()

    def test_a_file_test_case_is_refused(self, tmp_path):
        """This pass is about a program made of several files."""
        src, db = build(tmp_path, {'main.cpp': 'int main() { return 0; }\n'})
        pass_ = ReachabilityPass(
            arg=None, external_programs={'nm': shutil.which('nm'), 'readelf': shutil.which('readelf')}, compilation_database=str(db)
        )
        assert pass_.new(src / 'main.cpp') is None

    def test_without_a_database_nothing_is_proposed(self, tmp_path):
        src, _ = build(tmp_path, {'main.cpp': 'int main() { return 0; }\n'})
        pass_ = ReachabilityPass(arg=None, external_programs={}, compilation_database=None)
        assert pass_.generate_hints(src).hints == []
