"""Reject a bad candidate as cheaply as it can be rejected.

A reduction spends nearly all of its time answering one question about one
changed file, and the honest answer for most candidates is "this does not even
compile". Asking that question with a full build is paying minutes for an
answer that costs milliseconds: the flags for every file are already known, so
the file can be put to the compiler on its own.

So a candidate is examined in three stages, cheapest first, and only what
survives all of them costs a build:

  1. -fsyntax-only on the file that changed, with the flags its own build uses.
     Most rejected candidates die here, in the time it takes to parse one unit.
  2. the same unit compiled to an object, which is where anything the front end
     accepted but the back end will not appears.
  3. the project built the way the project is built -- cmake --build, which is
     ninja, which rebuilds exactly what changed -- and then the user's question
     about the result.

Only the third stage knows what "interesting" means, and only the third stage
belongs to the user. The first two are the compiler's own opinion of the file,
and C-Vise already has everything needed to ask for it.
"""

import logging
import subprocess
import tempfile
from pathlib import Path


class Verdict:
    """What a stage concluded, and why -- the reason is for the log, not for logic."""

    INTERESTING = 'interesting'
    REJECTED = 'rejected'
    UNDECIDED = 'undecided'


def syntax_check(command: list[str], env: dict, timeout: float | None = None) -> bool:
    """Does the changed file still parse, with the flags its build uses?"""
    argv = [a for a in command if a != '-c'] + ['-fsyntax-only']
    return _run(argv, env, timeout)


def object_check(command: list[str], env: dict, timeout: float | None = None) -> bool:
    """Does it still compile all the way to an object?

    Kept separate from the syntax check because it is perhaps ten times dearer
    and answers a different question: everything the front end accepted but the
    back end rejects -- and, for a reduction, that is a real category, since a
    pass will happily produce something that parses and cannot be emitted.
    """
    with tempfile.TemporaryDirectory(prefix='cvise-object-') as tmp:
        argv = [a for a in command if a != '-fsyntax-only']
        if '-c' not in argv:
            argv.append('-c')
        argv += ['-o', str(Path(tmp) / 'candidate.o')]
        return _run(argv, env, timeout)


def _run(argv: list[str], env: dict, timeout: float | None) -> bool:
    try:
        proc = subprocess.run(argv, env=env, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logging.debug('a precheck timed out: %s', argv[0])
        # Not an answer about the candidate, so it must not be read as one; the
        # caller treats this like any other stage that could not decide.
        return True
    except OSError:
        return True
    return proc.returncode == 0
