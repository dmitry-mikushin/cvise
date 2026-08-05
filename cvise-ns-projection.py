#!/usr/bin/env python3
"""Reduce ns-projection. Every choice is made here, none is asked.

The choices are not preferences. Each was paid for, and getting one wrong does
not produce an error -- it produces a reduction that runs for hours and answers
a question nobody asked.

WHY IT ALL HAPPENS IN THE PROJECT'S OWN CONTAINER
    C-Vise's C++ passes parse each file with clang_delta, and they are only as
    good as the flags they parse with -- which live in the build's
    compile_commands.json and name container paths and a container clang.
    MEASURED, on cbgraph_test.cpp and bbg.cpp: clang_delta on the host, against
    a path-rewritten copy of that database, gave 5 crashing transformations and
    ZERO usable ones; inside the build's own environment, against the database
    as written, 1 crash and 32 transformations finding 2900+ instances. A host
    run is not the same thing more slowly. It is a reduction with its semantic
    passes switched off, which still finishes and still prints a percentage.

    So C-Vise runs inside ns-rtc-cvise, which is the project's build image plus
    C-Vise (docker/ns-rtc/Dockerfile). A candidate is built by the ordinary
    `cmake --build` of this project, in that container. There is no second
    container per candidate, no docker socket, and no generated shell: jobs are
    kept apart by C-Vise's own overlay, and the build and the test are C-Vise's
    own work.

WHY THE TOP-LEVEL CMakeLists AND NOT THE ENGINE'S
    Because that is the configuration that has the tests. cpp/test is added by
    ns-nvrtc, after the targets it inspects exist -- nsopt_ns_schemas,
    ns_schemas_cpp, ns_projection_proto_adapter, nuvend_softfeu. Configuring
    the engine alone produces no tests at all, and therefore no criterion.

    The consequence is stated rather than hidden: the reducible set is
    everything the top-level database names, not only cpp/src and cpp/include.

WHY A GIT WORKTREE PINNED TO HEAD
    The reduced tree IS the worktree -- C-Vise rewrites it in place as soon as a
    smaller interesting variant is found, so an interrupted run loses nothing
    and the user's own checkout is never touched. It belongs to its pin: the
    criterion is that pin's behaviour, so continuing against a moved HEAD would
    grade the reduced tree by a different oracle.

WHY THE STATE IS IN /dev/shm AND NOT /tmp
    Both are tmpfs, and only one of them is for this. /tmp here is deliberately
    sized at 16 GiB, for programs' own scratch; /dev/shm is 126 GiB. Dozens of
    jobs each holding a build directory do not fit in the first and would starve
    everything else that legitimately uses it.

    In memory either way, because a build directory is written and read
    constantly and has no reason to touch a disk. Durability is not a reason to
    leave: the worktree is a git worktree, so what survives a reboot is what has
    been committed.

WHY ccache IS GIVEN A DIRECTORY OF ITS OWN
    The project's preset drives the compiler through ccache, and the image sets
    CCACHE_DIR to /src/.ccache -- inside the tree being reduced, which is the
    overlay's root. Every write then goes to the job's own delta, so ccache
    creates the whole chain of directories afresh for each object it stores:
    MEASURED, nine mkdir calls where an ordinary filesystem needs none, times
    81 jobs times every compiler invocation. A system-wide profile put 62% of
    the machine in ccache doing mkdir, 43% of all cycles in mkdir alone, and
    3.6% in anything useful.

    The cache was also worthless there: a delta belongs to one candidate and is
    thrown away with it, so nothing stored in it can ever be read back. Moved
    outside the overlay it is shared by every job and can actually hit.

WHAT MAKES A CANDIDATE INTERESTING
    That the project builds and one named ctest test still passes. The test is
    registered individually, its pass condition is stated in the project's
    CMakeLists rather than taken from an exit code -- a GoogleTest binary whose
    filter matches nothing prints "[  PASSED  ] 0 tests." and exits ZERO -- and
    its own definition is marked CVISE_NOREDUCE, so a candidate that empties it
    is refused before it is built.

    The question underneath is which code produces the output. Asking it
    through a test is a way of asking it, and the assumption it rests on is
    worth stating: the test must assert enough that code which changes the
    output makes it fail. Where it asserts less, the surviving set comes out
    smaller than the truth.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# The image is the project's build image plus C-Vise. Not configurable: a build
# from a different image is not this project's build, and a C-Vise from
# anywhere else parses with the wrong clang.
IMAGE = 'ns-rtc-cvise'
DOCKERFILE = 'Dockerfile.cvise'  # lives in the ns-nvrtc checkout, next to its own Dockerfile

# Inside the container the repository is always here, because that is what the
# compile_commands.json of the build says.
SRC = '/src'
SUBMODULE = 'third_party/ns-projection'

# The test that decides. Named, not discovered: a reduction graded by "some
# test passes" reduces to whichever test is cheapest to keep.
TEST = 'IngestTest.ParsesRepresentativeRequestJson'

# One job is one incremental rebuild plus one test run: the compile is
# single-threaded and dominates, so the CPU ceiling is nproc. Memory is the
# other ceiling -- the compiler itself was MEASURED at 689 MiB peak RSS on
# run_grouping.cpp, the heaviest unit, and each job materialises the part of
# the build its candidate changed. Two gigabytes covers it, and only three
# quarters of what is available is spent: a machine driven into swap reduces
# nothing.
JOB_BUDGET_MB = 2048


def die(message: str) -> None:
    print(f'cvise-ns-projection: {message}', file=sys.stderr)
    raise SystemExit(1)


def available_mb() -> int:
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) // 1024
    return 0


def derive_jobs() -> int:
    # One core short of the machine, deliberately. C-Vise sizes the shared
    # build pool as cores minus jobs, because each build already holds one
    # implicit token; asking for every core leaves the pool empty and every
    # candidate then compiles one file at a time.
    cpu = max(1, (os.cpu_count() or 1) - 1)
    memory = available_mb() * 3 // 4 // JOB_BUDGET_MB
    jobs = min(cpu, memory)
    if jobs < 1:
        die(f'less than {JOB_BUDGET_MB} MB available -- cannot run even one job')
    print(f'    jobs     : {jobs}  (cpu {cpu}, memory allows {memory} at {JOB_BUDGET_MB} MB/job)')
    return jobs


def find_repo() -> tuple[Path, Path]:
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / SUBMODULE / 'cpp' / 'src').is_dir():
            return candidate, candidate / SUBMODULE
    die('run this from inside an ns-nvrtc checkout (no third_party/ns-projection above the cwd)')


def check_image() -> None:
    if shutil.which('docker') is None:
        die('docker is not installed, and everything here happens in a container')
    if subprocess.run(['docker', 'image', 'inspect', IMAGE], capture_output=True).returncode != 0:
        die(f"image '{IMAGE}' not found -- build it from a C-Vise checkout with:\n"
            f'    docker build -t {IMAGE} -f $(ns-nvrtc)/Dockerfile.cvise $(cvise checkout)')


def prepare_worktree(submodule: Path, worktree: Path, resume: bool) -> str:
    pin = subprocess.run(['git', '-C', str(submodule), 'rev-parse', 'HEAD'],
                         capture_output=True, text=True).stdout.strip()
    if not worktree.exists():
        subprocess.run(['git', '-C', str(submodule), 'worktree', 'prune'], capture_output=True)
        subprocess.run(['git', '-C', str(submodule), 'worktree', 'add', '--detach',
                        str(worktree), pin], check=True, capture_output=True)
        print(f'    worktree : created at {pin}')
        return pin

    have = subprocess.run(['git', '-C', str(worktree), 'rev-parse', 'HEAD'],
                          capture_output=True, text=True).stdout.strip()
    if have != pin:
        if not resume:
            die(f'the worktree is at {have}, not the current {pin} -- remove {worktree}, '
                'or pass --resume to continue on its own pin')
        print(f'    NOTE     : the submodule moved to {pin}; continuing on {have},')
        print('               because the criterion belongs to that tree.')
        pin = have
    if resume:
        print('    worktree : reused as C-Vise left it')
    else:
        print('    worktree : reused; resetting it to the pristine pin')
        subprocess.run(['git', '-C', str(worktree), 'checkout', '--', '.'], check=True)
        subprocess.run(['git', '-C', str(worktree), 'clean', '-fd'],
                       check=True, capture_output=True)
    return pin


def remove_state(root: Path) -> None:
    """Remove the state, from inside the container that made it.

    The build directory is written by the container as root, so a host-side
    rm -rf gets Permission denied on most of the tree and leaves a
    half-deleted state that the next run treats as usable.
    """
    if not root.exists():
        return
    print(f'    clearing : {root}')
    subprocess.run(['docker', 'run', '--rm', '-v', f'{root.parent}:/state',
                    IMAGE, 'rm', '-rf', f'/state/{root.name}'], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('name', nargs='?', default='ns-projection',
                        help='run label; state lives in /dev/shm/cvise-<name>')
    parser.add_argument('--jobs', type=int, help='override the derived job count')
    parser.add_argument('--resume', action='store_true',
                        help='continue on the worktree as C-Vise left it')
    parser.add_argument('--fresh', action='store_true',
                        help='discard the state and start over')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the command that would run, and stop')
    args = parser.parse_args()

    if not re.fullmatch(r'[A-Za-z0-9._-]+', args.name):
        # It becomes a directory that a root container later removes. Anything
        # that is not plainly a name has no business being carried there.
        die(f'the run label {args.name!r} may only contain letters, digits, dot, dash '
            'and underscore')

    check_image()
    repo, submodule = find_repo()

    root = Path(f'/dev/shm/cvise-{args.name}')
    if args.fresh:
        remove_state(root)
        subprocess.run(['git', '-C', str(submodule), 'worktree', 'prune'], capture_output=True)
    worktree, tmp, ccache = root / 'ns-projection', root / 'tmp', root / 'ccache'
    for directory in (root, tmp, ccache):
        directory.mkdir(parents=True, exist_ok=True)

    print('=== cvise ns-projection reduction')
    print(f'    image    : {IMAGE}')
    print(f'    repo     : {repo}')
    print(f'    criterion: the project builds and ctest -R ^{TEST}$ passes')
    print(f'    state    : {root}')
    jobs = args.jobs or derive_jobs()
    pin = prepare_worktree(submodule, worktree, args.resume)
    print(f'    pin      : {pin}')

    ceiling_mb = available_mb() * 3 // 4
    command = [
        'docker', 'run', '--rm',
        # The reduction's own cgroup ceiling cannot engage in here -- inside the
        # container it is already at the root of its hierarchy and has nothing
        # to bound itself under -- so the bound is put on the container.
        '--memory', f'{ceiling_mb}m',
        # Equal to --memory, so the ceiling is a ceiling. Left larger, the
        # cgroup may swap instead of refusing: on this machine a run with
        # --memory 179G --memory-swap 358G exhausted 28.6 GB of swap, put 3539
        # tasks in uninterruptible sleep and 2543 of them in one disk queue,
        # reached a load of 3583 with the CPU idle, and had to be killed from
        # another machine. An honest OOM kills one candidate; eternal direct
        # reclaim kills the host.
        '--memory-swap', f'{ceiling_mb}m',
        '-v', f'{repo}:{SRC}',
        '-v', f'{worktree}:{SRC}/{SUBMODULE}',
        # At an identical path on both sides, so that anything C-Vise writes
        # down about where it put things is true outside the container too.
        '-v', f'{root}:{root}',
        '-e', f'TMPDIR={tmp}',
        # Outside the overlay root, so ccache writes once to a shared cache
        # instead of rebuilding its directory tree inside every job's delta.
        '-e', f'CCACHE_DIR={ccache}',
        '-e', 'CCACHE_MAXSIZE=20G',
        # The test target carries a precompiled header, and without this ccache
        # refuses to use its own entries for anything built with one: MEASURED,
        # 891 calls missed with "Could not use precompiled header" while the
        # rest of the run hit 86.67% of the time.
        '-e', 'CCACHE_SLOPPINESS=pch_defines,time_macros',
        '-w', SRC, IMAGE,
        # Configured from the root, because that is the only configuration in
        # which this component's tests exist at all -- cpp/test asks whether
        # targets the root creates are defined. Reduced under the component,
        # because the question is about the component. MEASURED when the two
        # were the same: 3651 translation units instead of 378, every job
        # copying 6375 files and 1485 directories, 39% of the machine in mkdir
        # and 4% doing useful work, and no verdict at all in 80 minutes.
        'cvise', '--n', str(jobs), '--under', SUBMODULE,
        f'{SRC}/CMakeLists.txt', TEST,
    ]

    if args.dry_run:
        print('=== would run:')
        print('   ', ' '.join(command))
        return 0

    print('=== running')
    print(f'    the reduced tree IS {worktree} -- C-Vise rewrites it in place as')
    print('    soon as a smaller interesting variant is found, so an interrupted run')
    print('    loses nothing and --resume continues from there.')
    return subprocess.run(command).returncode


if __name__ == '__main__':
    sys.exit(main())
