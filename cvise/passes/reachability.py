"""Empty the files the test cannot reach.

The call graph is not something to derive from the sources. It is already in the
object files -- as the symbols each one defines and the symbols each one needs
-- and following it from the object that holds `main` gives the set of files the
program can possibly use. Everything else contributes nothing to this test, and
its contents can go in one step.

That is a much better starting point than guessing, and it costs nothing extra:
the objects are the ones the baseline build has already produced. MEASURED on
ns-projection, ten hours into a reduction: 557 objects, 106 reachable from the
test, 451 not. The passes that were running had emptied files one at a time and
reached 39.5% in ten hours; this proposes the other 451 at once.

It also handles a function used only through a pointer without having to think
about it. Taking an address is a reference like any other, so the symbol appears
in the relocations and the object stays reachable. A source-level analysis would
have to reason about that and would get it wrong somewhere.

What this is NOT is a proof. An object with a dynamic initialiser runs its
initialiser whether anything calls into it or not -- that is how GoogleTest
registers a test case -- so an unreachable object can still matter. This pass
therefore proposes; the build and the test decide, exactly as for every other
pass, and the hint machinery bisects the set when the whole of it is too much.
"""

import collections
import json
import logging
import subprocess
from pathlib import Path

from cvise.passes.hint_based import HintBasedPass
from cvise.utils.hint import Hint, HintBundle, Patch

# Symbol classes nm reports for something an object provides. Weak and common
# ones count: another object linking against them is satisfied by this one.
DEFINING = set('TtWwVvDdBbRrGgSs')


class ReachabilityPass(HintBasedPass):
    """Propose emptying every file the test's own object cannot reach."""

    def __init__(self, compilation_database: str | None = None, **kwargs):
        super().__init__(compilation_database=compilation_database, **kwargs)
        self._compilation_database = compilation_database

    def check_prerequisites(self):
        return self.check_external_program('nm') and self.check_external_program('readelf')

    def supports_dir_test_cases(self):
        return True

    def new(self, test_case: Path, *args, **kwargs):
        if not test_case.is_dir():
            return None
        return super().new(test_case, *args, **kwargs)

    def generate_hints(self, test_case: Path, *args, **kwargs):
        if not self._compilation_database or not test_case.is_dir():
            return HintBundle(hints=[])
        source_of = self._objects_to_sources(test_case)
        if not source_of:
            return HintBundle(hints=[])

        unreachable = self._unreachable(sorted(source_of))
        vocabulary: list[bytes] = []
        hints: list[Hint] = []
        for obj in unreachable:
            relative = source_of[obj]
            path = test_case / relative
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size == 0:
                continue  # already empty; nothing to propose
            vocabulary.append(str(relative).encode())
            hints.append(
                Hint(patches=(Patch(path=len(vocabulary) - 1, left=0, right=size),))
            )

        logging.info(
            '%d of %d compiled files are not reachable from the test',
            len(hints),
            len(source_of),
        )
        return HintBundle(hints=hints, vocabulary=vocabulary)

    def _objects_to_sources(self, test_case: Path) -> dict[Path, Path]:
        """Which object the build makes from which file of the test case.

        Taken from the compilation database, which records it, rather than
        guessed from the object's path -- the two agree for a simple project and
        stop agreeing the moment anything is generated or renamed.
        """
        try:
            entries = json.loads(Path(self._compilation_database).read_text())
        except (OSError, json.JSONDecodeError) as e:
            logging.warning('cannot read %s: %s', self._compilation_database, e)
            return {}

        root = test_case.resolve()
        mapping: dict[Path, Path] = {}
        for entry in entries:
            output = entry.get('output')
            source = entry.get('file')
            if not output or not source:
                continue
            obj = Path(entry.get('directory', '.')) / output
            path = Path(source)
            if not path.is_absolute():
                path = Path(entry.get('directory', '.')) / path
            path = path.resolve()
            if not path.is_relative_to(root):
                continue
            mapping[obj.resolve()] = path.relative_to(root)
        return mapping

    def _unreachable(self, objects: list[Path]) -> list[Path]:
        """Everything the objects holding `main` cannot get to.

        Reachability is computed over every object the build produced, not only
        the ones under the test case: a path from the test through a vendored
        library and back into the project is a real path, and stopping at the
        library's edge would call the far side dead.
        """
        build = self._build_dir()
        every = sorted(build.rglob('*.o')) if build else objects
        defines: dict[str, list[Path]] = collections.defaultdict(list)
        needs: dict[Path, set[str]] = {}
        roots: list[Path] = []

        for obj in every:
            defined, needed = self._symbols(obj)
            needs[obj] = needed
            for symbol in defined:
                defines[symbol].append(obj)
            if 'main' in defined or self._runs_at_startup(obj):
                roots.append(obj)

        if not roots:
            logging.debug('no object defines main; not proposing anything')
            return []

        reached = set(roots)
        queue = list(roots)
        while queue:
            obj = queue.pop()
            for symbol in needs.get(obj, ()):
                for provider in defines.get(symbol, ()):
                    if provider not in reached:
                        reached.add(provider)
                        queue.append(provider)
        return [obj for obj in objects if obj not in reached]

    def _runs_at_startup(self, obj: Path) -> bool:
        """Does this object have code that runs before main, whoever calls it?

        A dynamic initialiser is entered unconditionally, so the object is a
        root: nothing has to reference it for its code to execute. This is what
        registers a GoogleTest case, and without it the pass proposes emptying
        the file that defines the very test being preserved.
        """
        try:
            proc = subprocess.run(
                [self.external_programs.get('readelf') or 'readelf', '-S', str(obj)],
                capture_output=True,
                text=True,
            )
        except OSError:
            return True  # cannot tell, so do not propose removing it
        return '.init_array' in proc.stdout or '.ctors' in proc.stdout

    def _build_dir(self) -> Path | None:
        """Where the objects are: the directory holding C-Vise's database."""
        if not self._compilation_database:
            return None
        # <build>/cvise_compile_commands/compile_commands.json
        build = Path(self._compilation_database).resolve().parent.parent
        return build if build.is_dir() else None

    def _symbols(self, obj: Path) -> tuple[set[str], set[str]]:
        try:
            proc = subprocess.run(
                [self.external_programs.get('nm') or 'nm', '--no-demangle', str(obj)],
                capture_output=True,
                text=True,
            )
        except OSError:
            return set(), set()
        defined, needed = set(), set()
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == 'U':
                needed.add(parts[1])
            elif len(parts) == 3 and parts[1] in DEFINING:
                defined.add(parts[2])
        return defined, needed
