"""Make a candidate's build be about the candidate.

A verdict that rests on the previous candidate's binary is a verdict about code
this candidate never contained, and it does not announce itself: the build
succeeds, the test passes, and the reduction accepts. MEASURED on ns-projection,
99 verdicts in one run with this shape:

    build=0 rebuilt=NO ninja_idle=yes delta_files=1345 whiteouts=966
    deleted=hidden overlay_env=3

966 headers deleted, the deletion correctly hidden from the job -- and ninja
said there was nothing to do. Of the 377 compiled files, only 4 are unreachable
from the test, so removing almost every header must break the build. It did not,
because nothing asked it to.

The reason is what ninja records. A source file that changes gets a new
timestamp and is rebuilt; a source file that VANISHES is not a target, not an
input to any live edge, and ninja concludes there is nothing to do. It is right
about its own graph and wrong about the question being asked.

So this deletes the objects that depended on what the candidate removed, before
the build runs. Then a deleted header produces a real compile error, which is an
honest rejection, and a candidate that removed nothing anybody used rebuilds
nothing and stays cheap.

It also refuses to let the answer be silent: if objects were deleted, the test
binary MUST come back newer, and if it does not, something rebuilt it from
somewhere this program cannot see -- a stale cache, an overlay that did not
reach the compiler -- and that is reported rather than graded.
"""

import re
import subprocess
import sys
from pathlib import Path

WHITEOUT_SUFFIX = '.cvise-whiteout'


def removed_by(delta: Path) -> list[str]:
    """The paths this candidate deleted, as the build knows them."""
    gone = []
    for marker in delta.rglob('*' + WHITEOUT_SUFFIX):
        inside = str(marker)[len(str(delta)) :]
        gone.append(inside[: -len(WHITEOUT_SUFFIX)])
    return gone


def objects_depending_on(build: Path, paths: set[str]) -> list[str]:
    """Which built objects listed any of these as a dependency.

    Asked of ninja, which is the only thing that knows: the dependencies of a
    C++ object are discovered by the compiler and recorded in .ninja_deps, not
    derivable from the sources without compiling them.
    """
    try:
        deps = subprocess.run(
            ['ninja', '-t', 'deps'], cwd=build, capture_output=True, text=True, timeout=600
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    hits, current = [], None
    for line in deps.splitlines():
        if not line.startswith(' ') and ':' in line:
            current = line.split(':', 1)[0]
        elif current and line.strip() in paths:
            hits.append(current)
            current = None  # one hit is enough to condemn the object
    return hits


def main() -> int:
    if len(sys.argv) != 3:
        print('usage: invalidate.py <build-dir> <delta-dir>', file=sys.stderr)
        return 2
    build, delta = Path(sys.argv[1]), Path(sys.argv[2])
    if not delta.is_dir():
        print('0')  # nothing was delivered, so nothing can be stale
        return 0

    gone = set(removed_by(delta))
    if not gone:
        # Modifications carry a new timestamp through the overlay and ninja
        # rebuilds them by itself. Only disappearance is invisible to it.
        print('0')
        return 0

    removed = 0
    for obj in objects_depending_on(build, gone):
        target = build / obj
        try:
            target.unlink()
            removed += 1
        except OSError:
            pass
    print(removed)
    return 0


if __name__ == '__main__':
    sys.exit(main())
