#!/usr/bin/env python3
"""Reduce the ns-projection engine. Every choice is made here, none is asked.

This exists because the choices are not preferences. Each one below was paid
for, and getting any of them wrong does not produce an error -- it produces a
reduction that runs for hours and answers a question nobody asked.

WHY EVERYTHING RUNS IN A CONTAINER
    C-Vise's C++ passes parse each file with clang_delta, and they are only as
    good as the flags they parse with -- which live in the build's
    compile_commands.json and name container paths and a container clang.
    MEASURED, on cbgraph_test.cpp and bbg.cpp: clang_delta on the host, against
    a path-rewritten copy of that database, gave 5 crashing transformations and
    ZERO usable ones; inside the build's own environment, against the database
    as written, it gave 1 crash and 32 transformations finding 2900+ instances.
    A host run is therefore not "the same but slower". It is a reduction with
    its semantic passes switched off, which still finishes and still prints a
    percentage.

    So C-Vise runs in ns-rtc-cvise (ns-rtc plus C-Vise built against the very
    same clang-19), and each candidate is built by a sibling container from the
    plain ns-rtc image -- an ordinary build of this project, not a special one.

WHY THE STATE IS IN /tmp
    /tmp here is a tmpfs sized for exactly this. Every job owns a build
    directory and a source tree, and both are written and read constantly. On a
    disk filesystem the reduction is I/O bound on a workload that has no reason
    to touch a disk at all. Durability is not the reason to leave: the worktree
    is a git worktree, so what survives a reboot is what has been committed.

WHY A GIT WORKTREE PINNED TO HEAD
    The reduced tree IS the worktree -- C-Vise rewrites it in place as soon as a
    smaller interesting variant is found, so an interrupted run loses nothing.
    It belongs to its pin: the criterion is that pin's behaviour, so continuing
    against a moved HEAD would grade the reduced tree by a different oracle and
    silently invalidate every earlier decision.

WHY THE BUILD SLOTS ARE CLONED
    A fresh build directory has to build the whole tree (nsopt, nsai_runtime,
    next32-backend) before it can link the unit tests, so N fresh slots means N
    full builds. Slot 0 is configured from the project's own preset and built
    once; the rest are copied from it inside the container, and every slot is
    mounted at the SAME container path /build, so the absolute paths baked into
    build.ninja and CMakeCache.txt are identical across slots.

WHY THE CANDIDATE IS RSYNC'ED BY CONTENT
    A fresh copy carries fresh timestamps, ninja would rebuild all 236
    translation units for every variant, and a reduction that pays a full
    rebuild per candidate does not finish. --checksum gives a new timestamp only
    to what really changed.

WHAT MAKES A CANDIDATE INTERESTING
    That one named test still passes, asked of ctest by name. The test is
    registered individually (gtest_discover_tests), its pass condition is stated
    in the project's CMakeLists rather than taken from an exit code -- a
    GoogleTest binary whose filter matches nothing prints "[  PASSED  ] 0
    tests." and exits ZERO -- and the test's own definition is marked
    CVISE_NOREDUCE so a candidate that empties it is refused before it is built.

    The older harness used a different oracle: the canonical sha256 that
    canonical_parity_test.cpp prints for a fixture, which is the same oracle the
    corpus ledger uses. That one answers "which code produces this output"; this
    one answers "which code this test needs". Both are legitimate; they are not
    the same question, and mixing them would give an answer to neither.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# The two images, and what each is for. Not configurable: a build from a
# different image is not this project's build, and a C-Vise from anywhere but
# the reduction image parses with the wrong clang.
BUILD_IMAGE = 'ns-rtc'
CVISE_IMAGE = 'ns-rtc-cvise'

# Inside the container the repository is always here, because that is what the
# compile_commands.json of every build slot says.
SRC = '/src'
SUBMODULE_IN_SRC = 'third_party/ns-projection'
BUILD_IN_CONTAINER = '/build'

# The test that decides. Named, not discovered: a reduction graded by "some test
# passes" reduces to whichever test is cheapest to keep.
TEST = 'IngestTest.ParsesRepresentativeRequestJson'
TEST_TARGET = 'ns_projection_unit_tests'

# What is offered for deletion: the code, all of it.
#
# The whole tree and not one file, because a criterion's real footprint is
# spread across headers, templates and sibling translation units, and a per-file
# run can only delete what one file happens to contain.
#
# The tests are in it. Leaving them out would protect the criterion by keeping
# its file out of reach, which is protection by scope -- the reducer would be
# free to hollow the test out the moment anyone widened the list, and nothing
# would say so. The criterion is protected instead by the CVISE_NOREDUCE marker
# on its own definition, which is checked for every candidate in every mode. The
# other 235 test files genuinely are not needed by it, and deleting them is a
# real answer rather than a hole in the question.
#
# cpp/fixtures is NOT here, and that is a statement about what it is rather than
# about protecting anything: the fixtures are the input the criterion consumes.
# Reducing them would change the question instead of answering it.
REDUCIBLE = ('cpp/src', 'cpp/include', 'cpp/test', 'cpp/adapter', 'cpp/edsl')

# One job is one incremental rebuild plus one test run: the compile is
# single-threaded and dominates, so the CPU ceiling is nproc. Memory is the
# other ceiling and has three terms -- a private build directory on tmpfs
# (MEASURED ~1000 MB once built), a private copy of the reducible tree (15 MB),
# and the compiler itself (MEASURED peak RSS 689 MiB on run_grouping.cpp, the
# heaviest unit). Two gigabytes covers all three, and only three quarters of
# what is available is spent: a machine driven into swap reduces nothing.
JOB_BUDGET_MB = 2048

# A timed-out test is scored NOT INTERESTING, so a valid-but-slow candidate is
# silently thrown away and the run quietly loses power. This is a correctness
# knob, not a convenience one: ten times an uncontended iteration, floored high
# enough that contention cannot masquerade as "not interesting".
TIMEOUT_FACTOR = 10
TIMEOUT_FLOOR = 900


def die(message: str) -> None:
    print(f'cvise-ns-projection: {message}', file=sys.stderr)
    raise SystemExit(1)


def run(command, **kwargs):
    return subprocess.run(command, **kwargs)


def docker(*args, **kwargs):
    return run(['docker', *args], **kwargs)


def available_mb() -> int:
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) // 1024
    return 0


def derive_jobs() -> int:
    cpu = os.cpu_count() or 1
    memory = available_mb() * 3 // 4 // JOB_BUDGET_MB
    jobs = min(cpu, memory)
    if jobs < 1:
        die(f'less than {JOB_BUDGET_MB} MB available -- cannot run even one job')
    print(f'    jobs     : {jobs}  (cpu {cpu}, memory allows {memory} at {JOB_BUDGET_MB} MB/job)')
    return jobs


def find_repo() -> tuple[Path, Path]:
    """The ns-nvrtc checkout and the ns-projection submodule inside it."""
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        submodule = candidate / SUBMODULE_IN_SRC
        if (submodule / 'cpp' / 'src').is_dir():
            return candidate, submodule
    die('run this from inside an ns-nvrtc checkout (no third_party/ns-projection above the cwd)')


def check_images() -> None:
    for image in (BUILD_IMAGE, CVISE_IMAGE):
        if docker('image', 'inspect', image, capture_output=True).returncode != 0:
            die(f"image '{image}' not found -- build it first (./build_docker.sh)")


def prepare_worktree(submodule: Path, worktree: Path, resume: bool) -> str:
    pin = run(['git', '-C', str(submodule), 'rev-parse', 'HEAD'],
              capture_output=True, text=True).stdout.strip()
    if not worktree.exists():
        run(['git', '-C', str(submodule), 'worktree', 'add', '--detach', str(worktree), pin],
            check=True, capture_output=True)
        print(f'    worktree : created at {pin}')
        return pin

    have = run(['git', '-C', str(worktree), 'rev-parse', 'HEAD'],
               capture_output=True, text=True).stdout.strip()
    if have != pin:
        if not resume:
            die(f'the worktree is at {have}, not the current {pin} -- remove {worktree}, '
                'or pass --resume to continue on its own pin')
        print(f'    NOTE     : the submodule moved to {pin} since this run started;')
        print(f'               continuing on the worktree pin {have}, because the')
        print('               criterion belongs to that tree.')
        pin = have
    if resume:
        print('    worktree : reused as C-Vise left it')
    else:
        print('    worktree : reused; resetting it to the pristine pin')
        run(['git', '-C', str(worktree), 'checkout', '--', *REDUCIBLE], check=True)
        run(['git', '-C', str(worktree), 'clean', '-fd', *REDUCIBLE],
            check=True, capture_output=True)
    return pin


def build_mounts(repo: Path, worktree: Path, build: Path) -> list[str]:
    return [
        '-v', f'{repo}:{SRC}',
        '-v', f'{worktree}:{SRC}/{SUBMODULE_IN_SRC}',
        '-v', f'{build}:{BUILD_IN_CONTAINER}',
    ]


def configure_slot_zero(submodule: Path, worktree: Path, builds: Path, log: Path) -> None:
    """Configure the reference slot with the project's own script.

    Not with `cmake --preset default -B ...`: the preset's binaryDir is fixed to
    ${sourceDir}/build and cannot be redirected, so a private build directory has
    to replay the preset's cacheVariables explicitly. The project already has a
    script that reads them out of CMakePresets.json at run time, and says why it
    is a script rather than something each caller writes out: "a second
    hand-written copy of the replay is exactly how the two would silently
    diverge". A build directory configured any other way grades candidates under
    flags the project does not use.
    """
    print('    configuring reference slot 0 ...')
    script = submodule / 'scripts' / 'configure_build_dir.sh'
    if not script.is_file():
        die(f'{script} is missing, and it is what knows how to configure a private build dir')
    (builds / '0').mkdir(parents=True, exist_ok=True)
    proc = run([str(script), str(worktree), str(builds / '0')],
               capture_output=True, text=True, env={**os.environ, 'IMAGE': BUILD_IMAGE})
    log.write_text(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        print((proc.stdout + proc.stderr)[-2000:], file=sys.stderr)
        die(f'configure of slot 0 failed (see {log})')


def build_slot_zero(repo: Path, worktree: Path, builds: Path, log: Path) -> None:
    print('    building reference slot 0 (full tree, once) ...')
    proc = docker(
        'run', '--rm', *build_mounts(repo, worktree, builds / '0'),
        '-w', SRC, BUILD_IMAGE,
        'cmake', '--build', BUILD_IN_CONTAINER, '--target', TEST_TARGET,
        capture_output=True, text=True,
    )
    log.write_text(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        print((proc.stdout + proc.stderr)[-2000:], file=sys.stderr)
        die(f'the baseline build failed (see {log})')


def clone_slots(builds: Path, jobs: int) -> None:
    """Copy slot 0 into the pool, inside the container.

    The build directories are root-owned, because the container builds as root,
    so a host-side copy hits "Permission denied" on most of the tree.
    """
    print(f'    cloning slot 0 into {jobs} job slots ...')
    script = (
        'set -e\n'
        f'for slot in $(seq 1 {jobs}); do\n'
        '  [ -f /builds/$slot/build.ninja ] && continue\n'
        '  rm -rf /builds/$slot /builds/$slot.tmp\n'
        '  cp -a /builds/0 /builds/$slot.tmp\n'
        '  mv /builds/$slot.tmp /builds/$slot\n'
        'done\n'
    )
    if docker('run', '--rm', '-v', f'{builds}:/builds', BUILD_IMAGE,
              'bash', '-c', script).returncode != 0:
        die('cloning a build slot failed (out of tmpfs?)')


def seed_trees(worktree: Path, trees: Path, jobs: int) -> None:
    print(f'    seeding {jobs} source trees ...')
    for slot in range(1, jobs + 1):
        for part in REDUCIBLE:
            destination = trees / str(slot) / Path(part).name
            destination.mkdir(parents=True, exist_ok=True)
            run(['rsync', '-a', '--delete', f'{worktree / part}/', f'{destination}/'], check=True)


def write_interestingness(root: Path, repo: Path, worktree: Path, jobs: int) -> Path:
    """The question asked of every candidate, and where it is asked.

    It runs inside the reduction image and builds in a sibling container from
    the plain build image, so a candidate is graded by an ordinary build of this
    project rather than by anything this program invented.
    """
    names = ' '.join(Path(part).name for part in REDUCIBLE)
    # One read-only mount per reducible directory, over the place the build
    # expects it. Read-only because the build has no business writing into what
    # is being reduced, and a job that did would poison the next candidate.
    mounts = ' \\\n    '.join(
        f'-v "$ROOT/trees/$slot/{Path(part).name}:{SRC}/{SUBMODULE_IN_SRC}/{part}:ro"'
        for part in REDUCIBLE
    )
    script = f'''#!/bin/bash
# Generated by cvise-ns-projection -- do not edit by hand.
# Interesting (exit 0) iff the candidate tree builds and {TEST} still passes.
set -u
ROOT={root}
REPO={repo}
WT={worktree}
IMAGE={BUILD_IMAGE}
NJOBS={jobs}

for part in {names}; do [ -d "$PWD/$part" ] || exit 1; done

slot=""
for try in $(seq 1 3000); do
    for i in $(seq 1 "$NJOBS"); do
        exec {{fd}}>"$ROOT/pool/$i.lock"
        if flock -n "$fd"; then slot=$i; break; fi
        exec {{fd}}>&-
    done
    [ -n "$slot" ] && break
    sleep 0.2
done
[ -n "$slot" ] || exit 1

# By content: only what the candidate really changed gets a new timestamp, so
# ninja rebuilds only those translation units. Copying instead would restamp
# every file and cost a full rebuild per candidate.
for part in {names}; do
    rsync -a --delete --checksum "$PWD/$part/" "$ROOT/trees/$slot/$part/" \\
        || {{ flock -u "$fd"; exit 1; }}
done

t0=$(date +%s)
docker run --rm \\
    -v "$REPO:{SRC}" \\
    -v "$WT:{SRC}/{SUBMODULE_IN_SRC}" \\
    {mounts} \\
    -v "$ROOT/builds/$slot:{BUILD_IN_CONTAINER}" \\
    -w {SRC} "$IMAGE" bash -c "
        cmake --build {BUILD_IN_CONTAINER} --target {TEST_TARGET} >/tmp/b.log 2>&1 || exit 3
        ctest --test-dir {BUILD_IN_CONTAINER} -R '^{TEST}$' --no-tests=error \\
              --output-on-failure >/tmp/t.log 2>&1" >/dev/null 2>&1
rc=$?
t1=$(date +%s)

flock -u "$fd"; exec {{fd}}>&-

case $rc in
  0) verdict=KEEP ;;
  3) verdict=BUILD_FAILED ;;
  *) verdict=TEST_FAILED ;;
esac
bytes=$(du -sb {' '.join(f'"$PWD/{Path(p).name}"' for p in REDUCIBLE)} | awk '{{s+=$1}} END {{print s}}')
printf '%s slot=%s dt=%s bytes=%s\\n' "$verdict" "$slot" "$((t1 - t0))" "$bytes" \\
    >> "$ROOT/stats.log"

[ "$verdict" = KEEP ] && exit 0
exit 1
'''
    path = root / 'interesting.sh'
    path.write_text(script)
    path.chmod(0o755)
    return path


def cvise_in_container(root: Path, repo: Path, worktree: Path, tmp: Path, command: list[str]):
    """Run something in the reduction image.

    $ROOT is mounted at the IDENTICAL path on both sides, so the paths C-Vise
    hands to the interestingness test are also valid as sibling-container mount
    sources; and the worktree is mounted where the build expects it, so the
    paths in compile_commands.json resolve for clang_delta too.
    """
    return docker(
        'run', '--rm',
        '-v', '/var/run/docker.sock:/var/run/docker.sock',
        '-v', f'{repo}:{SRC}',
        '-v', f'{worktree}:{SRC}/{SUBMODULE_IN_SRC}',
        '-v', f'{root}:{root}',
        '-e', f'TMPDIR={tmp}',
        '-w', str(root), CVISE_IMAGE, *command,
    )


def self_check(root: Path, repo: Path, worktree: Path, tmp: Path, script: Path) -> int:
    """The pristine tree must be interesting, or every step is rejected for the
    wrong reason and the run reduces nothing while looking busy."""
    print('    self-check: the pristine tree must be interesting ...')
    area = tmp / 'selfcheck'
    for part in REDUCIBLE:
        destination = area / Path(part).name
        destination.mkdir(parents=True, exist_ok=True)
        run(['rsync', '-a', '--delete', f'{worktree / part}/', f'{destination}/'], check=True)
    import time
    started = time.monotonic()
    proc = cvise_in_container(root, repo, worktree, tmp,
                              ['bash', '-c', f'cd {area} && {script}'])
    if proc.returncode != 0:
        die(f'the UNMODIFIED tree is not interesting -- the criterion is wrong '
            f'(see {root}/stats.log)')
    took = int(time.monotonic() - started)
    print(f'    self-check: OK (one uncontended iteration: {took} s)')
    return took


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('name', nargs='?', default='ns-projection',
                        help='run label; state lives in /tmp/cvise-<name>')
    parser.add_argument('--jobs', type=int, help='override the derived job count')
    parser.add_argument('--resume', action='store_true',
                        help='continue on the worktree as C-Vise left it')
    parser.add_argument('--setup-only', action='store_true',
                        help='prepare everything and print the command, do not reduce')
    args = parser.parse_args()

    if shutil.which('docker') is None:
        die('docker is not installed, and every build here happens in a container')
    check_images()
    repo, submodule = find_repo()

    root = Path(f'/tmp/cvise-{args.name}')
    worktree, builds, trees, tmp, pool = (
        root / 'ns-projection', root / 'builds', root / 'trees', root / 'tmp', root / 'pool')
    for directory in (root, builds, trees, tmp, pool):
        directory.mkdir(parents=True, exist_ok=True)

    print('=== cvise ns-projection reduction (whole tree, in the project container)')
    print(f'    repo     : {repo}')
    print(f'    criterion: ctest -R ^{TEST}$')
    print(f'    state    : {root}')
    jobs = args.jobs or derive_jobs()
    pin = prepare_worktree(submodule, worktree, args.resume)
    print(f'    pin      : {pin}')

    configure_slot_zero(submodule, worktree, builds, root / 'configure_0.log')
    build_slot_zero(repo, worktree, builds, root / 'baseline_build.log')
    clone_slots(builds, jobs)
    seed_trees(worktree, trees, jobs)
    print(f'    pool     : {jobs} warm build slots and source trees under {root}')

    script = write_interestingness(root, repo, worktree, jobs)
    took = self_check(root, repo, worktree, tmp, script)
    timeout = max(took * TIMEOUT_FACTOR, TIMEOUT_FLOOR)
    print(f'    timeout  : {timeout} s per test')

    command = [
        'cvise', '--n', str(jobs), '--timeout', str(timeout),
        # Without this every clang_delta pass parses with a bare default
        # invocation that cannot even find the project's headers, and the C++
        # passes -- the ones that delete functions, classes and whole files --
        # contribute nothing. MEASURED: 0 usable transformations without it, 32
        # with it.
        '--compilation-database', str(builds / '0'),
        str(script),
        *(f'{SRC}/{SUBMODULE_IN_SRC}/{part}' for part in REDUCIBLE),
    ]

    if args.setup_only:
        print('=== setup only; run it with:')
        print(f'    docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \\')
        print(f'        -v {repo}:{SRC} -v {worktree}:{SRC}/{SUBMODULE_IN_SRC} \\')
        print(f'        -v {root}:{root} -e TMPDIR={tmp} -w {root} {CVISE_IMAGE} \\')
        print(f'        {" ".join(command)}')
        return 0

    print(f'=== running (per-iteration log: {root}/stats.log)')
    print(f'    the reduced tree IS {worktree}/cpp/{{src,include}} -- C-Vise rewrites it')
    print('    in place as soon as a smaller interesting variant is found, so an')
    print('    interrupted run loses nothing and --resume continues from there.')
    return cvise_in_container(root, repo, worktree, tmp, command).returncode


if __name__ == '__main__':
    sys.exit(main())
