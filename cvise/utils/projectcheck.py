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
import contextlib
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from cvise.utils import invalidate
from cvise.utils.fileutil import KEEP_MARKER
from cvise.utils.project import build_failure_report


DELTA = '.cvise-delta'

# Not 1, which the project's test uses for "not interesting", and not 0. A
# candidate whose verdict could not be established is neither.
UNDECIDABLE = 125

# How many processes a candidate may have at once before it is refused.
#
# The number separates two things that differ by orders of magnitude, so its
# exact value does not matter much -- only that it sits between them. Legitimate
# parallelism in this test binary is bounded by hardware_concurrency, 88 on this
# machine, and the check invokes ctest with an exact single filter, which takes
# the in-process path and needs a handful. MEASURED against a candidate that
# went wrong: 43 332 processes.
PROCESS_CEILING = 256

# How often the swarm is looked for. A bomb reaches thousands in seconds, so
# this is fast enough to catch it while costing one /proc scan per second.
SWARM_POLL_SECONDS = 1.0


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


def keep_this_job(why: str) -> None:
    """Ask for this job's directory to survive, because its verdict was not ordinary.

    Almost every candidate is rejected for a reason that is fully described by
    the one line in the witness log, and its directory answers nothing that the
    line does not. Keeping all of them is not a diagnostic aid: MEASURED on
    ns-projection, 3403 of them came to 112 GB and filled a 126 GB tmpfs, which
    is RAM here, and the run had to be killed with none of them ever opened.

    The few worth opening are the ones whose verdict was not ordinary, and only
    this program knows which those are. C-Vise sees an exit code, and a build
    that failed and a test that failed are both "nonzero" -- the distinction
    exists here and nowhere else, so it is recorded here.
    """
    try:
        (Path.cwd() / KEEP_MARKER).write_text(why + '\n')
    except OSError:
        pass  # a lost marker costs a directory, never a verdict


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


def my_process_group() -> list[int]:
    """Every process in this candidate's group, ours included.

    Exact rather than approximate, and that is the point of it. Since C-Vise
    starts each candidate with start_new_session, this check leads a process
    group that contains its build, its test, and everything either of them
    spawned -- and nothing else on the machine, however busy the machine is.
    Counting descendants instead would ask a question the kernel stops
    answering the moment a parent dies.
    """
    mine = os.getpgrp()
    found = []
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            if os.getpgid(int(entry)) == mine:
                found.append(int(entry))
        except (ProcessLookupError, PermissionError, ValueError):
            continue
    return found


def disperse(members: list[int]) -> None:
    """Kill the group, one by one, sparing this process.

    Not killpg: that would include us, and then nothing would be left to write
    the verdict -- which is the whole reason for noticing.
    """
    for pid in members:
        if pid == os.getpid():
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


def watch_for_a_swarm(ceiling: int, done: threading.Event) -> list[int]:
    """Refuse a candidate that spawns without bound, while it is still cheap.

    MEASURED, and it cost eleven hours. A candidate's test binary re-executed
    itself; 43 332 of its processes were left holding 134 GiB, and the run kept
    going for an hour before it could no longer run the test at all. The
    candidate was never accepted -- the test could not decide, and an undecided
    candidate keeps its previous state -- so correctness was never at stake.
    Availability was: the run died of it.

    Killing the swarm from outside is a cure for the symptom. Refusing the
    candidate that produced it is the verdict the reduction actually needs, and
    it belongs here, where the candidate is.
    """
    seen: list[int] = []
    while not done.wait(SWARM_POLL_SECONDS):
        members = my_process_group()
        if len(members) > ceiling:
            seen = members
            disperse(members)
            return seen
    return seen


# "100% tests passed, 0 tests failed out of 3" -- the last number is how many
# tests ctest actually found, and it is the only thing that distinguishes a
# criterion that held from one that was deleted out from under it.
RAN = re.compile(r'\bout of (\d+)\b')


def they_did_not_all_run(output: str, wanted: int) -> str:
    """Why this candidate must be refused even though ctest exited zero.

    Returns the reason, or '' when all the named tests ran.

    --no-tests=error is not enough once there is more than one name. MEASURED,
    two tests asked for and one of them deleted:

        ctest -R '^(alpha|deleted)$' --no-tests=error
        100% tests passed, 0 tests failed out of 1        rc=0

    ctest ran what it found, all of it passed, and it said so with a zero exit.
    The flag only fires when NOTHING matched. So the surviving test would have
    spoken for the deleted one for the rest of the run, and the cheapest way to
    satisfy a two-test criterion would be to delete one of the tests.

    The count in ctest's own summary is what closes it, and nothing else in the
    output does.
    """
    if wanted <= 1:
        # One name is already covered by --no-tests=error, which turns "not
        # found" into exit 8. Nothing to add, and a summary line that ctest
        # someday words differently must not start failing single-test runs.
        return ''
    found = RAN.search(output)
    if not found:
        return f'ctest did not say how many of the {wanted} tests it ran'
    ran = int(found.group(1))
    if ran < wanted:
        return f'only {ran} of the {wanted} tests the criterion names still exist'
    return ''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--build', required=True, type=Path)
    parser.add_argument('--target')
    parser.add_argument('--test', required=True, action='append', dest='tests',
                        help='exact ctest name; repeat to require several, all of '
                             'which must pass')
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
            keep_this_job(f'undecided: {killed} objects removed, no binary to compare')
            # Two absences compare equal, so there is nothing here to compare.
            # "I could not tell" must not be recorded as "it passed", which is
            # the very substitution this check exists to prevent.
            print(f'cvise: {killed} objects were removed but the binary under test was')
            print('cvise: not there before the build, so this candidate cannot be shown')
            print('cvise: to have been compiled at all.')
            return UNDECIDABLE
        if before == after:
            record(args.witness, **facts, test='undecided', rc='-', dir=Path.cwd())
            # The signature of a build that could not see the candidate's
            # deletions, which is the failure this whole check exists for.
            keep_this_job(f'undecided: {killed} objects removed and the binary did not change')
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
    # Watched while it runs rather than inspected afterwards: a candidate that
    # spawns without bound has to be refused while refusing it is still cheap.
    done = threading.Event()
    swarm: list[int] = []

    def watch() -> None:
        nonlocal swarm
        swarm = watch_for_a_swarm(PROCESS_CEILING, done)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        proc = subprocess.run(
            [
                'ctest',
                '--test-dir',
                str(args.build),
                '-R',
                '^(' + '|'.join(re.escape(name) for name in args.tests) + ')$',
                '--no-tests=error',
                '--output-on-failure',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    finally:
        done.set()
        watcher.join(timeout=SWARM_POLL_SECONDS * 3)

    if swarm:
        record(args.witness, **facts, test='swarm', rc=len(swarm), dir=Path.cwd())
        keep_this_job(f'swarm: {len(swarm)} processes in this candidate group')
        # UNDECIDABLE, not "not interesting". What this candidate does to the
        # machine says nothing about whether the code it deleted mattered, and
        # recording it as uninteresting would throw away a reduction on the
        # strength of an accident. The undecided budget is what stops a run
        # where this keeps happening.
        print(f'cvise: this candidate had {len(swarm)} processes at once, past the')
        print(f'cvise: ceiling of {PROCESS_CEILING}. They have been killed. A candidate')
        print('cvise: that spawns without bound cannot be judged on this machine.')
        return UNDECIDABLE
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
    missing = they_did_not_all_run(proc.stdout, len(args.tests))
    record(
        args.witness,
        **facts,
        test='pass' if proc.returncode == 0 and not missing else 'fail',
        rc=proc.returncode,
        dir=Path.cwd(),
    )
    if missing:
        keep_this_job(missing)
        print(proc.stdout, end='')
        print(f'cvise: {missing}')
        # Not the test's verdict but the criterion's: fewer tests exist than
        # were asked for, so what did run cannot speak for what did not.
        return 1
    if proc.returncode != 0:
        # A test that ran and failed is the interesting rejection: the candidate
        # compiled, so the code is well-formed, and it changed behaviour. That is
        # the one a person has to look at, and looking needs the directory.
        keep_this_job(f'test failed with {proc.returncode}')
    print(proc.stdout, end='')
    return proc.returncode


if __name__ == '__main__':
    sys.exit(main())
