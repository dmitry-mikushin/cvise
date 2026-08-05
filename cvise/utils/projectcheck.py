"""Deciding whether a candidate is interesting, as a program rather than a text.

C-Vise's contract with the user is an executable: it runs one file in each
job's directory and reads the exit status. In project mode nobody writes that
file, because the project already says what to build and what to check, so
C-Vise supplies it.

What it must not do is supply it as a shell script assembled from Python
strings. That is a program written in a language the interpreter that produced
it cannot check: quoting is by hand, a branch is untested until it is reached in
a live run, and the diagnostics degrade to `cat`. It had already cost the
project once -- the generated script printed the raw build log, reintroducing
exactly the failure `build_failure_report` exists to prevent, because a shell
string cannot call a Python function.

So the generated file is one `exec` line carrying arguments and no logic, and
everything it decides is here, where it is ordinary code with ordinary tests.

Three things happen, in this order, and the order is the point:

  * the objects that depended on what the candidate deleted are removed, so
    that a deletion the build system cannot see becomes a compile error it can;
  * the build runs, and if it fails the failure is reported by the same code
    that reports every other build failure;
  * the binary under test is required to have changed if anything was removed,
    because a test run against the previous candidate's binary returns a
    verdict about code this candidate never contained.

Only then is the project's own test asked.
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

from cvise.utils import invalidate
from cvise.utils.project import build_failure_report


DELTA = '.cvise-delta'

# Not 1, which the project's test uses for "not interesting", and not 0. A
# candidate whose verdict could not be established is neither.
UNDECIDABLE = 125


def binary_under_test(build: Path, target: str | None) -> Path | None:
    """Where the executable the test runs actually landed.

    Asked of the tree rather than computed, because a target's output path is a
    property of the generator's layout and not of its name.
    """
    if not target:
        return None
    for candidate in build.rglob(target):
        if candidate.is_file():
            return candidate
    return None


def signature(path: Path | None) -> str | None:
    """Enough of a file's identity to tell a rebuild from an absence.

    Modification time and size together, because a rebuild that produces
    byte-identical output still moves the time, and a file that is being written
    while this reads it may share a time with the one it replaced.
    """
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return f'{stat.st_mtime_ns}-{stat.st_size}'


def build(build_dir: Path, target: str | None) -> tuple[int, str]:
    command = ['cmake', '--build', str(build_dir)]
    if target:
        command += ['--target', target]
    proc = subprocess.run(command, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def edges_run(output: str) -> int:
    """How much the build actually did.

    "The binary did not change" has two causes that are repaired in opposite
    ways and that the verdict alone cannot tell apart: a build that compiled
    nothing, and a build that compiled a great deal and produced the same bytes.
    A live run gave 69 verdicts and 69 said the binary had not changed, and
    there was no way to say which of the two it was -- the build's output is
    thrown away on success, so the one number that distinguishes them was never
    written down.

    Ninja numbers each edge it starts, `[k/n]`, so the largest k it reached is
    that number, and it costs a regular expression over output already in hand.
    """
    numbered = re.findall(r'^\[(\d+)/(\d+)\]', output, re.M)
    return max((int(k) for k, _ in numbered), default=0)


def record(witness: Path | None, **facts: object) -> None:
    """What a verdict rested on, written down as it is made.

    A reduction that accepts a candidate it never built is the one failure that
    looks like success, and afterwards there is nothing left to examine: the
    job's directory is gone, and reconstructing the moment from outside
    reconstructs a different moment. Seven such reconstructions in a row all
    held while the run itself did not.
    """
    if witness is None:
        return
    line = ' '.join(f'{key}={value}' for key, value in facts.items())
    try:
        with witness.open('a') as log:
            log.write(line + '\n')
    except OSError:
        pass  # a lost record must not change a verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--build', required=True, type=Path)
    parser.add_argument('--target')
    parser.add_argument('--test', required=True, help='exact ctest name')
    parser.add_argument('--witness', type=Path)
    args = parser.parse_args(argv)

    binary = binary_under_test(args.build, args.target)
    before = signature(binary)

    killed = invalidate.remove_stale(args.build, Path.cwd() / DELTA)
    started = time.monotonic()
    returncode, output = build(args.build, args.target)
    seconds = time.monotonic() - started
    after = signature(binary)

    # Everything known so far, so that a candidate whose build failed leaves a
    # record even though it never reaches the test. `test` says so rather than
    # being absent, because an absent field reads as an unnoticed omission.
    facts = dict(
        build=returncode,
        killed=killed,
        edges=edges_run(output),
        secs=f'{seconds:.1f}',
        rebuilt='no' if before == after else 'yes',
    )

    if returncode != 0:
        record(args.witness, **facts, test='notrun', rc='-', dir=Path.cwd())
        print(build_failure_report(output, args.build))
        return returncode

    if killed:
        if before is None:
            record(args.witness, **facts, test='undecided', rc='-', dir=Path.cwd())
            # Two absences compare equal, so there is nothing here to compare.
            # "I could not tell" must not be recorded as "it passed", which is
            # the very substitution this check exists to prevent.
            print(f'cvise: {killed} objects were removed but the binary under test was')
            print('cvise: not there before the build, so this candidate cannot be shown')
            print('cvise: to have been compiled at all.')
            return UNDECIDABLE
        if before == after:
            record(args.witness, **facts, test='undecided', rc='-', dir=Path.cwd())
            # Something rebuilt it from a source this program cannot see -- a
            # cache that kept the old object, an overlay that never reached the
            # compiler. A verdict on that binary is a verdict about whichever
            # candidate did produce it.
            print(f'cvise: {killed} objects were removed and {binary} did not change;')
            print("cvise: this candidate would have been judged by another one's binary.")
            return UNDECIDABLE

    # Both streams, merged, because ctest writes the failing test's output to
    # one and its own summary to the other, and a report missing either half is
    # the report of a failure nobody can act on.
    proc = subprocess.run(
        [
            'ctest',
            '--test-dir',
            str(args.build),
            '-R',
            '^' + re.escape(args.test) + '$',
            '--no-tests=error',
            '--output-on-failure',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Written after the test, not before it.
    #
    # The record used to be made as soon as the build finished, which left the
    # one outcome that decides everything out of it: whether the candidate is
    # interesting. A run then showed 36 records saying the build succeeded and
    # the binary changed, against nothing published at all, and there was no way
    # to tell an honestly failing test from a publication that had stopped
    # working -- two faults repaired in opposite directions. Reported as
    # "36 accepts" until the published tree was looked at, which is a guess
    # dressed as a measurement.
    record(
        args.witness,
        **facts,
        test='pass' if proc.returncode == 0 else 'fail',
        rc=proc.returncode,
        dir=Path.cwd(),
    )
    print(proc.stdout, end='')
    return proc.returncode


if __name__ == '__main__':
    sys.exit(main())
