"""Reject a bad candidate as cheaply as it can be rejected -- while it is cheap.

A reduction spends nearly all of its time answering one question about one
changed file, and the honest answer for most candidates is "this does not even
compile". Asking that with a full build pays for a link to learn something a
parse would have said: the flags for every file are already known, so the file
can be put to the compiler on its own.

So a candidate is examined in two stages, and only what survives the first
costs a build:

  1. -fsyntax-only on the file that changed, with the flags its own build uses.
     Most rejected candidates die here, in the time it takes to parse one unit.
  2. the project built the way the project is built -- cmake --build, which is
     ninja, which rebuilds exactly what changed -- and then the user's question
     about the result.

Only the second stage knows what "interesting" means, and only it belongs to
the user. The first is the compiler's own opinion of the file, and C-Vise
already has everything needed to ask for it.

There used to be a stage between them: the same unit compiled all the way to an
object, to catch what the front end accepts and the back end will not. It is
gone, because it duplicated the build almost exactly. MEASURED on
ns-projection: parsing a unit 2.3 s, compiling it 6.4 s, building and linking a
one-file candidate 16 s. It therefore cost a whole compile on every candidate
that survived the parse -- which is most of those that get that far -- to save
a 9 s link in the rare case of a failure only the back end sees. The build
catches those one step later, and the build is the authority regardless.

The stage that remains is worth it only while the file it asks about is the one
file that changed. That condition lives with the caller, which is what knows
how many files a candidate touched.
"""

import logging
import subprocess


class Verdict:
    """What a stage concluded, and why -- the reason is for the log, not for logic."""

    INTERESTING = 'interesting'
    REJECTED = 'rejected'
    UNDECIDED = 'undecided'


def syntax_check(command: list[str], env: dict, timeout: float | None = None) -> bool:
    """Does the changed file still parse, with the flags its build uses?"""
    argv = [a for a in command if a != '-c'] + ['-fsyntax-only']
    return _run(argv, env, timeout)


class Undecided(Exception):
    """The stage could not run, which is not an answer about the candidate."""


def _run(argv: list[str], env: dict, timeout: float | None) -> bool:
    try:
        proc = subprocess.run(argv, env=env, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Saying "it passed" here is how a machine under load quietly turns
        # into a reducer that accepts things nobody checked; saying "it failed"
        # is how it turns into one that discards good candidates. Neither is an
        # answer, so neither is given.
        logging.debug('a precheck timed out: %s', argv[0])
        raise Undecided(f'{argv[0]} timed out')
    except OSError as e:
        raise Undecided(f'{argv[0]} could not be run: {e}')
    return proc.returncode == 0
