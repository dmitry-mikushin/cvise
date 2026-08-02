"""Say which files the build refers to, so that nothing tries to delete them.

The pass that removes files removes whatever no other pass has claimed with a
"@fileref" hint. Headers are claimed by whoever includes them, and files named
in a Makefile are claimed by the Makefile -- but a translation unit of a CMake
project is claimed by nobody, because the thing that names it is the
CMakeLists.txt, and the CMakeLists.txt is not under reduction. It cannot be:
the reduction is of the code, not of the build definition.

So every round proposed deleting every source file, and every one of those
candidates was refused -- not by a judgement about the code, but by ninja
declining to build an edge whose input is gone:

    ninja: error: 'unit00.cpp', needed by 'CMakeFiles/prog.dir/unit00.cpp.o',
           missing and no known rule to make it

MEASURED on twelve translation units: 66 attempts, 0 successes. Each is cheap
on its own -- ninja stops while reading the manifest, in about 30 ms -- but the
count is the number of source files times the number of rounds, and on a real
project that is thousands of candidates whose answer was known before they were
scheduled.

Nothing had to be guessed to fix it. The compilation database already names
exactly those files, which is the same fact the Makefile pass reports for a
Makefile, in the same form.
"""

import json
import logging
from pathlib import Path

from cvise.passes.hint_based import HintBasedPass
from cvise.utils.hint import Hint, HintBundle

FILEREF = b'@fileref'


class CompilationDatabasePass(HintBasedPass):
    """Reports every file the build compiles as referred-to, and nothing else.

    It proposes no transformation of its own. "@fileref" is a special hint type,
    never applied to a test case, so this pass costs one enumeration and no
    candidates at all.
    """

    def __init__(self, compilation_database: str | None = None, **kwargs):
        super().__init__(compilation_database=compilation_database, **kwargs)
        self._compilation_database = compilation_database

    def check_prerequisites(self):
        return True

    def supports_dir_test_cases(self):
        return True

    def output_hint_types(self) -> list[bytes]:
        return [FILEREF]

    def generate_hints(self, test_case: Path, *args, **kwargs):
        if not self._compilation_database or not test_case.is_dir():
            return HintBundle(hints=[])
        try:
            entries = json.loads(Path(self._compilation_database).read_text())
        except (OSError, json.JSONDecodeError) as e:
            # The database is C-Vise's own and is written before any pass runs,
            # so this cannot happen quietly; say so rather than silently going
            # back to proposing deletions that cannot work.
            logging.warning('cannot read %s: %s', self._compilation_database, e)
            return HintBundle(hints=[])

        vocabulary: list[bytes] = [FILEREF]
        hints: list[Hint] = []
        seen: set[Path] = set()
        # The test case is named relative to the directory C-Vise works in,
        # while the database names absolute paths, and a relative path is under
        # nothing at all.
        root = test_case.resolve()
        for entry in entries:
            try:
                path = Path(entry['file'])
            except (KeyError, TypeError):
                continue
            # Only the files the build compiles. An entry with no "output" is a
            # header: C-Vise gives those a compile command so that the semantic
            # passes can work on them, but nothing in the build names a header,
            # so a header that nobody includes any more really can be deleted --
            # and claiming it here would take that away.
            if not entry.get('output'):
                continue
            if not path.is_absolute():
                path = Path(entry.get('directory', root)) / path
            path = path.resolve()
            # A file the database names but the test case does not hold is one
            # the reduction cannot delete in any case.
            if not path.is_relative_to(root):
                continue
            relative = path.relative_to(root)
            if relative in seen:
                continue
            seen.add(relative)
            vocabulary.append(str(relative).encode())
            hints.append(Hint(type=0, extra=len(vocabulary) - 1))

        logging.debug('the build refers to %d files of the test case', len(hints))
        return HintBundle(hints=hints, vocabulary=vocabulary)
