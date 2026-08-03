"""Parts of the project a reduction is not allowed to touch.

A reduction is graded by one question -- does the interestingness test still
pass -- and that makes the test itself the one thing it must not be free to
edit. Deleting the test outright is caught by the test command: an exact ctest
filter that matches nothing is an error. Hollowing it out is caught by nothing,
because a test whose assertions have been removed still compiles, still
registers and still passes. MEASURED on ns-projection, each with a real build:

    intact      ctest: PASSED
    hollowed    ctest: PASSED      <- and it never could tell
    deleted     ctest: refused (exit 8)

Everything reduced after such a candidate is measured against a criterion that
is no longer there, and the run does not fail. It succeeds, spectacularly, at
producing an answer about nothing.

So the protection has to be written down somewhere, and there is only one place
it can honestly live. Not in a flag naming files to skip: the whole interface
of this program is that CMake already knows which files there are, and a second
list is a second answer that can disagree with the build. Not in per-pass
cooperation either -- that is only as strong as the pass that forgets, and a
pass that forgets does not fail loudly, it produces a candidate that looks like
progress.

It lives in the source, on the definition it protects:

    CVISE_NOREDUCE
    void IngestTest_ParsesRepresentativeRequestJson_Test::TestBody() { ... }

which the project defines as [[clang::annotate("cvise::noreduce")]], or as
nothing under compilers that have no such attribute. Either spelling is fine
here, because what is read is the source and not the preprocessed translation
unit.

It has to go on a definition written out rather than on TEST(suite, name),
which has nowhere to put it. MEASURED, both compilers, both placements:

    after TEST(a,b)   g++   attributes are not allowed on a function-definition
                      clang 'clang::annotate' cannot be applied to types
    before TEST(a,b)  g++   expected unqualified-id before 'static_assert'
                      clang an attribute list cannot appear here

The macro expands to a class, a registration and an out-of-line TestBody(), and
begins with a static_assert, so there is no attributable declaration at either
end. Written out, the last of the three is an ordinary function definition and
takes an attribute like any other.

How far the definition reaches is asked of the parser, not counted in braces:
braces inside strings, character literals, raw strings and comments are not
braces, and the input here is by construction half-destroyed source. The marker
falls inside the node the parser reports, so removing the marker is itself a
change to the protected text and is refused like any other.

Nothing is remembered between generations. The regions are read afresh out of
each text, so there are no stored offsets to go stale as the file around them
shrinks.
"""

import functools
import json
import logging
import subprocess
from pathlib import Path

from cvise.utils.externalprograms import find_external_programs


MARKER = 'CVISE_NOREDUCE'


@functools.cache
def _lister() -> str | None:
    return find_external_programs().get('treesitter_delta')


def _marks(line: str) -> bool:
    """Is this line the marker, rather than prose or a definition of it?

    A use starts the line. Prose that merely names the marker -- a comment
    explaining why it is there, this docstring, the header that defines it --
    does not, and must not count: a mention inside a definition would otherwise
    protect something nobody meant to protect, and a mention in the defining
    header would refuse every candidate touching it forever.
    """
    return line.lstrip().startswith(MARKER)


def has_protection(text: str) -> bool:
    """Is the marker used here, as opposed to mentioned or defined here?"""
    return any(_marks(line) for line in text.splitlines())


def protected_regions(path: Path) -> list[str] | None:
    """The text of each marked definition, or None if that cannot be determined.

    None is not "nothing is protected". It is "this file says something is
    protected and I could not work out what", which has to be refused rather
    than waved through -- the whole point is that a silent failure here looks
    like a very good reduction.
    """
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError):
        return []
    if not has_protection(text):
        return []

    lister = _lister()
    if lister is None:
        logging.warning('%s marks a definition not to be reduced, but treesitter_delta '
                        'is not available to find how far it reaches', path)
        return None
    try:
        proc = subprocess.run(
            [lister, 'list-definitions', str(path)], capture_output=True, text=True
        )
    except OSError as e:
        # Refused rather than raised: this runs in a worker judging a candidate,
        # and an exception here would be reported as the candidate being
        # undecided rather than as the guard being unable to do its job.
        logging.warning('cannot run %s to find the protected definition in %s: %s',
                        lister, path, e)
        return None
    if proc.returncode != 0:
        return None

    regions = []
    for line in proc.stdout.splitlines():
        if not line.startswith('{'):
            continue  # the vocabulary line, which this transformation does not use
        try:
            span = json.loads(line)
        except ValueError:
            return None
        chunk = text[span['l'] : span['r']]
        if has_protection(chunk):
            regions.append(chunk)
    if not regions:
        # Marked, but the marker is not inside any definition: either it was put
        # somewhere that is not one, or the file no longer parses far enough to
        # say. Both are reasons to stop, not to continue unguarded.
        return None
    return regions


def disturbed(before: Path, after: Path) -> bool:
    """Did the candidate change something it was not allowed to change?"""
    original = protected_regions(before)
    if original is None:
        return True
    if not original:
        return False
    return protected_regions(after) != original
