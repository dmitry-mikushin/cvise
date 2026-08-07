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
import os
import re
import shutil
import subprocess
from pathlib import Path

from cvise.utils.externalprograms import find_external_programs


MARKER = 'CVISE_NOREDUCE'

# The same guard, naming the test it belongs to. Every test in a suite can carry
# one, and only the one whose name is the criterion this run was given does
# anything -- so which test is protected follows from which test is being graded
# by, rather than from editing a thousand files whenever that changes.
#
# It has to be visible in the SOURCE, which is why this is a second spelling
# rather than a preprocessor condition: what is read here is the text on disk,
# never the preprocessed translation unit, so an #if would leave every marker
# looking equally active.
MARKER_TEST = MARKER + '_TEST'


@functools.cache
def _lister() -> str | None:
    """Where treesitter_delta actually is, or None if it is nowhere.

    find_external_programs() seeds its map with the bare program name and
    replaces it only when it finds the file, so a missing tool comes back as
    the string 'treesitter_delta' rather than as None. Believing that string is
    what made the "not available" path below unreachable: absence turned into
    an OSError inside a worker and was reported as the user's test case being
    insane.
    """
    found = find_external_programs().get('treesitter_delta')
    if not found:
        return None
    if os.path.isabs(found):
        return found if os.path.exists(found) else None
    return shutil.which(found)


def _defines_marker(text: str) -> bool:
    """Does this file provide the marker rather than use it?

    The header that defines CVISE_NOREDUCE names it on every other line, and
    none of those lines begins with it, so the warning below would fire on the
    one file whose whole purpose is to mention it. MEASURED on a real run: 73
    of 103 log lines were that warning about that header, drowning everything
    the log was for -- including, on that occasion, whatever killed the run.

    A warning nobody can act on trains its reader to ignore the channel it
    arrives on, which costs more than it ever saves.
    """
    return re.search(rf'^\s*#\s*(?:define|undef|ifdef|ifndef)\s+{re.escape(MARKER)}',
                     text, re.M) is not None


def _marks(line: str, criterion: str | None = None) -> bool:
    """Is this line an ACTIVE marker, rather than prose or a definition of one?

    A use starts the line. Prose that merely names the marker -- a comment
    explaining why it is there, this docstring, the header that defines it --
    does not, and must not count: a mention inside a definition would otherwise
    protect something nobody meant to protect, and a mention in the defining
    header would refuse every candidate touching it forever.

    The named form is active only for the test this run is graded by. Without a
    criterion every marker is active, which is the safe direction: a guard that
    goes quiet when it does not know what it is guarding is worse than one that
    refuses too much.
    """
    line = line.lstrip()
    if line.startswith(MARKER_TEST):
        named = re.match(rf'{re.escape(MARKER_TEST)}\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)', line)
        if named is None:
            # Written like the named form and not parseable as it. Refusing to
            # guess which test it meant, and refusing to let it protect nothing.
            return True
        return criterion is None or f'{named.group(1)}.{named.group(2)}' == criterion
    return line.startswith(MARKER)


def has_protection(text: str, criterion: str | None = None) -> bool:
    """Is an active marker used here, as opposed to mentioned or defined here?"""
    return any(_marks(line, criterion) for line in text.splitlines())


def protected_regions(path: Path, criterion: str | None = None) -> list[str] | None:
    """The text of each marked definition, or None if that cannot be determined.

    None is not "nothing is protected". It is "this file says something is
    protected and I could not work out what", which has to be refused rather
    than waved through -- the whole point is that a silent failure here looks
    like a very good reduction.
    """
    path = Path(path)
    try:
        text = path.read_text()
    except FileNotFoundError:
        # A candidate may legitimately not have a file the original had. That
        # protects nothing, which differs from the original and is refused by
        # the comparison rather than here.
        return []
    except (OSError, UnicodeDecodeError):
        # Not the same thing at all: the file is there and cannot be read.
        # Answering "nothing is protected" would hand the reduction exactly the
        # permission this module exists to withhold.
        logging.warning('cannot read %s to find out what it protects', path)
        return None
    if not has_protection(text, criterion):
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
        if has_protection(chunk, criterion):
            regions.append(chunk)
    if not regions:
        # Marked, but the marker is not inside any definition: either it was put
        # somewhere that is not one, or the file no longer parses far enough to
        # say. Both are reasons to stop, not to continue unguarded.
        return None
    return regions


def disturbed(before: Path, after: Path, criterion: str | None = None) -> bool:
    """Did the candidate change something it was not allowed to change?"""
    original = protected_regions(before, criterion)
    if original is None:
        return True
    if not original:
        return False
    return protected_regions(after, criterion) != original


def _uses_marker(path: Path, criterion: str | None = None) -> bool:
    try:
        text = path.read_text()
    except UnicodeDecodeError:
        # Binary. A marker is a line of ASCII in a source file, so this one
        # carries none, and saying so is not a guess.
        return False
    except OSError as e:
        # Cannot tell. Counting it as unmarked would answer "nothing is
        # protected here" for a file nobody could read, which is the one answer
        # that must never be given on a guess.
        logging.warning('cannot read %s; treating it as protected because it '
                        'cannot be shown otherwise: %s', path, e)
        return True
    if has_protection(text, criterion):
        return True
    # `criterion=None` deliberately: the question here is whether any line
    # starts with a marker at all, not whether one applies to this run. A file
    # full of guards naming other tests is not a file where somebody wrote the
    # marker and got nothing -- MEASURED, the criterion-aware test warned about
    # 226 of 227 files at once, which is the log-drowning this warning was
    # narrowed to avoid.
    if MARKER in text and not _defines_marker(text) and not has_protection(text, None):
        # The word is there but no line begins with it, so nothing is protected
        # and the file looks exactly like one that never asked to be. Somebody
        # wrote the marker and got no guard, which is the failure this whole
        # module exists to prevent, arriving through the guard itself.
        logging.warning(
            '%s mentions %s but no line begins with it, so nothing in it is protected. '
            'A marker has to start its line; in prose or indented it does nothing.',
            path,
            MARKER,
        )
    return False


@functools.lru_cache(maxsize=None)
def marked_files(root: str, criterion: str | None = None) -> tuple[str, ...]:
    """Which files under a test case carry a marker, found once and remembered.

    Remembering is sound even though the tree shrinks underneath: a file cannot
    become marked, and one that stops being marked has had its marker removed,
    which is the thing being refused. Scanning the candidate instead would miss
    exactly that case -- a candidate that deleted the marker has nothing left to
    find.

    Worth remembering because the alternative is reading every file of the tree
    for every candidate, and all but one of them has nothing to say.
    """
    path = Path(root)
    if path.is_file():
        return (root,) if _uses_marker(path, criterion) else ()
    if not path.is_dir():
        return ()
    # os.walk with followlinks, not rglob: rglob does not descend into
    # symlinked directories, so a tree that reaches its sources through one --
    # which is how several of these projects are laid out -- would report no
    # marked files and protect nothing. Directories already visited are skipped
    # by identity, so a link that points back up cannot spin.
    found: list[str] = []
    seen: set[tuple[int, int]] = set()
    for directory, subdirectories, names in os.walk(path, followlinks=True):
        try:
            stat = os.stat(directory)
        except OSError:
            continue
        if (stat.st_dev, stat.st_ino) in seen:
            subdirectories[:] = []
            continue
        seen.add((stat.st_dev, stat.st_ino))
        for name in names:
            candidate = Path(directory) / name
            if candidate.is_file() and _uses_marker(candidate, criterion):
                found.append(str(candidate))
    return tuple(sorted(found))


def violation(original_root: Path, candidate_root: Path, criterion: str | None = None) -> Path | None:
    """The first marked file this candidate disturbed, or None if it left them alone.

    Takes roots rather than a list of changed files, so that it holds for a
    candidate produced any way at all: by patches, by clang_delta rewriting a
    whole file, or by a pass that simply deleted one.
    """
    original_root, candidate_root = Path(original_root), Path(candidate_root)
    if original_root.resolve() == candidate_root.resolve():
        # The two are the same file or tree, so every comparison below would
        # trivially agree and the guard would pass everything. Whatever put them
        # here, that answer is worthless and must not be mistaken for consent.
        raise ValueError(
            f'a candidate cannot be compared against itself: {original_root} is {candidate_root}'
        )
    directory = original_root.is_dir()
    for marked in marked_files(str(original_root), criterion):
        marked = Path(marked)
        candidate = (
            Path(candidate_root) / marked.relative_to(original_root)
            if directory
            else Path(candidate_root)
        )
        if disturbed(marked, candidate, criterion):
            return marked
    return None
