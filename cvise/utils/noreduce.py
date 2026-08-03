"""Parts of the project a reduction is not allowed to touch.

A reduction is graded by one question -- does the interestingness test still
pass -- and that makes the test itself the one thing it must not be free to
edit. Deleting the test outright is caught by the test command: an exact ctest
filter that matches nothing is an error. Hollowing it out is not caught by
anything, because a test whose assertions have been removed still passes, so a
reducer that empties it has found a candidate that satisfies every check and
means nothing. Everything after that point reduces against a criterion that is
no longer there, and the run does not fail -- it succeeds, spectacularly, at
producing an answer about nothing.

So the protection has to be expressed somewhere, and there is only one place it
can honestly live. Not in a command-line flag naming files to skip: the whole
of this program's interface is that CMake already knows which files there are,
and any second list is a second answer that can disagree with the build. Not in
per-pass cooperation either: that is only as strong as the pass that forgets,
and a pass that forgets does not fail loudly, it quietly produces a candidate
that looks like progress.

It lives in the source, next to what it protects, and it is enforced by
comparison rather than by cooperation. A marked region is read out of the file
the candidate was made from and out of the file the candidate is, and if the
two do not match, the candidate never gets built. No pass is asked for
anything, so no pass can forget; and because the regions are read afresh from
each text, there are no stored offsets to go stale as the file around them
shrinks.

The markers are comments, and paired:

    // cvise noreduce begin
    TEST(IngestTest, ParsesRepresentativeRequestJson) { ... }
    // cvise noreduce end

Not a C++ attribute, for a reason that is specific rather than stylistic: the
thing most worth protecting is a GoogleTest TEST(), which expands to a member
function definition with no syntactic room to attach an attribute to. And not
a single marker covering "the next declaration" either, because finding where
that declaration ends requires parsing, and the input here is by construction
half-destroyed source that a parser may no longer accept. Two comments need no
parser and are exact on any text at all.

The markers are inside the region they open and close, so deleting one is
itself a change to the protected text, and is refused like any other.
"""

OPEN = 'cvise noreduce begin'
CLOSE = 'cvise noreduce end'


def has_protection(text: str) -> bool:
    """Cheap enough to ask about every file of every candidate."""
    return OPEN in text


def protected_regions(text: str) -> list[str]:
    """The protected passages themselves, in order, markers included.

    Their content is returned rather than their offsets because offsets are not
    comparable between two different versions of a file: a pass that deletes a
    function above a protected region moves it, and moving it is allowed. What
    must not change is what it says.
    """
    regions: list[str] = []
    lines = text.splitlines(keepends=True)
    start = None
    position = 0
    offsets = []
    for line in lines:
        offsets.append(position)
        position += len(line)
    offsets.append(position)

    for index, line in enumerate(lines):
        if start is None:
            if OPEN in line:
                start = index
        elif CLOSE in line:
            regions.append(text[offsets[start] : offsets[index + 1]])
            start = None
    if start is not None:
        # An unterminated marker protects the rest of the file. The alternative
        # is to protect nothing, which turns a typo into a silently unguarded
        # criterion -- the exact failure this exists to prevent.
        regions.append(text[offsets[start] :])
    return regions


def disturbed(before: str, after: str) -> bool:
    """Did the candidate change anything it was not allowed to change?"""
    if not has_protection(before):
        return False
    return protected_regions(before) != protected_regions(after)
