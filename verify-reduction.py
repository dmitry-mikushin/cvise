#!/usr/bin/env python3
"""Does what the reduction has published so far still build and still pass?

A reduction reports its progress as a percentage, and a percentage is not an
answer to that question. One run reached 14.87 MB -> 6970 bytes in four minutes
and the result did not compile: every number it printed was about a tree nobody
had built.

So the tree is built. Not the one the jobs share -- that one has the baseline in
it and would answer about work already done -- but a fresh configure and build of
exactly what has been written back, in a directory of its own, followed by the
same ctest the reduction is graded by.

The reduction is PAUSED for the duration. Two builds of this project at once do
not fit on one machine, and a verification that competes with what it verifies
measures the contention rather than the tree. Pausing is safe: a candidate has a
budget of some twenty minutes and this takes two, and SIGCONT resumes every
process exactly where it stopped.

Nothing here writes into the reduction's state. The check has its own build
directory and its own ccache, so a failure to verify cannot damage the run that
is being verified.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path


IMAGE = 'ns-rtc-cvise'
SRC = '/src'
SUBMODULE = 'third_party/ns-projection'

# Where a verified result is put so that it survives the machine. The state
# directory is tmpfs and the reduction that made it can die at any moment; twice
# during one reduction the result was saved only because a copy had already
# landed here.
RESULTS = Path.home() / 'cvise-results'

# The commits are made by a program, so they say so rather than borrowing
# whoever happened to be logged in.
AUTHOR = ('-c', 'user.name=C-Vise', '-c', 'user.email=cvise@localhost')

SOURCE_SUFFIXES = ('.cpp', '.cc', '.cxx', '.c', '.hpp', '.hh', '.hxx', '.h', '.inc')


def container_of(state: Path) -> str | None:
    """The reduction working on this state directory, by what it has mounted.

    Found by its mounts rather than by its name or its image: a name is
    assigned at random and an image tag is shared by every run, and both have
    already been used to report a live run as dead.
    """
    listing = subprocess.run(
        ['docker', 'ps', '--format', '{{.ID}}'], capture_output=True, text=True
    ).stdout.split()
    for cid in listing:
        mounts = subprocess.run(
            ['docker', 'inspect', cid, '--format', '{{range .Mounts}}{{.Source}} {{end}}'],
            capture_output=True, text=True,
        ).stdout
        if str(state) in mounts.split():
            return cid
    return None


def run(command, **kwargs):
    return subprocess.run(command, capture_output=True, text=True, **kwargs)


def git(tree: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', str(tree), *args], capture_output=True, text=True)


def how_much_code(tree: Path) -> str:
    """What is left, in the terms a person judges a reduction by.

    Not bytes. They move the same for a stripped space and for a deleted
    translation unit, so a message carrying them says nothing about how far the
    reduction has come.
    """
    files = lines = 0
    for path in tree.rglob('*'):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES or '.git' in path.parts:
            continue
        try:
            counted = sum(1 for line in path.read_text(errors='replace').splitlines() if line.strip())
        except OSError:
            continue
        if counted:
            files += 1
            lines += counted
    return f'{lines} lines of code in {files} files'


def on_the_branch(tree: Path, branch: str) -> bool:
    """Put the verified history on a branch of its own, creating it once.

    The worktree is checked out detached at the pin, so the first commit has
    nowhere to go. Creating the branch at HEAD moves no file, which is why this
    is safe to do while the reduction is running -- and it was, seven times.
    """
    if git(tree, 'rev-parse', '--verify', branch).returncode == 0:
        return git(tree, 'checkout', branch).returncode == 0
    return git(tree, 'checkout', '-b', branch).returncode == 0


def preserve(tree: Path, state: Path, test: str) -> None:
    """Commit the verified tree and bundle it out of the tmpfs it lives in.

    VERIFIED is the moment the result is known to be good, and it is also the
    only moment at which that is cheap to record. MEASURED twice in one
    reduction: a run died at 56% with an exception while writing a crash
    report, and another was stopped by hand after converging -- in both cases
    the result survived because a copy had already been committed and bundled.
    Nothing else about the run survived: the state directory is tmpfs.

    A bundle rather than an archive, because it carries the history: the result
    is then a diff against the project's real HEAD rather than an opaque tree,
    and it is small -- 19 MB against a 91 MB checkout.

    `git add` and `git commit` change no file in the working tree, so this does
    not disturb a reduction that is running in it. That is the whole safety
    argument and it is worth stating, because the operation looks invasive and
    is not.
    """
    if git(tree, 'rev-parse', '--git-dir').returncode != 0:
        print(f'note: {tree} is not a git checkout, so the result was not preserved',
              file=sys.stderr)
        return

    branch = f'{state.name}-verified'
    if not on_the_branch(tree, branch):
        print(f'note: could not put the result on branch {branch}', file=sys.stderr)
        return

    git(tree, 'add', '-A')
    if not git(tree, 'diff', '--cached', '--quiet').returncode:
        # Nothing has changed since the last verified point. An empty commit
        # would say a reduction happened when none did.
        print(f'already preserved: {branch} is the tree that was just verified')
        return

    message = f'reduced by C-Vise: {how_much_code(tree)}, VERIFIED to build and pass {test}'
    committed = git(tree, *AUTHOR, 'commit', '-m', message)
    if committed.returncode != 0:
        print(f'note: the verified tree could not be committed: {committed.stderr.strip()[:200]}',
              file=sys.stderr)
        return

    RESULTS.mkdir(parents=True, exist_ok=True)
    bundle = RESULTS / f'{tree.name}-{state.name}-verified.bundle'
    # HEAD as well as the branch, and it is not redundant. A bundle holding only
    # a branch has no HEAD to check out, so `git clone` of it produces a
    # repository with an empty working tree and the warning "remote HEAD refers
    # to nonexistent ref, unable to checkout". MEASURED: the content is all
    # there, but getting at it takes a second, non-obvious step, and the clone
    # looks broken to whoever is handed it. With HEAD recorded, one clone gives
    # the tree, on the branch, with the files in it.
    made = subprocess.run(['git', '-C', str(tree), 'bundle', 'create', str(bundle), 'HEAD', branch],
                          capture_output=True, text=True)
    if made.returncode != 0:
        print(f'note: the bundle could not be written: {made.stderr.strip()[:200]}', file=sys.stderr)
        return
    # Verified rather than assumed: a bundle that cannot be read back is a
    # backup that only looks like one, and the day it is needed is the wrong
    # day to find out.
    checked = subprocess.run(['git', 'bundle', 'verify', str(bundle)], capture_output=True, text=True)
    if checked.returncode != 0:
        print(f'note: {bundle} does not verify: {checked.stderr.strip()[:200]}', file=sys.stderr)
        return

    revision = git(tree, 'rev-parse', '--short', 'HEAD').stdout.strip()
    size = bundle.stat().st_size / 1024 ** 2
    print(f'preserved: {revision} on {branch}, {message.split(", VERIFIED")[0]}')
    print(f'           {bundle} ({size:.1f} MB, history complete)')


def verify(state: Path, test: str, repo: Path, keep: bool) -> int:
    tree = state / 'ns-projection'
    if not tree.is_dir():
        print(f'no published tree at {tree}', file=sys.stderr)
        return 2

    # Every file in the checkout, which is NOT the number the reduction prints.
    # C-Vise counts only the files it is reducing; this counts fixtures, data
    # and .git as well, and the two differ by an order of magnitude -- 6534023
    # against 86773054 on the same tree. Printed as what it is, because it was
    # once printed beside a percentage and read as the reduction's own progress.
    size = sum(f.stat().st_size for f in tree.rglob('*') if f.is_file())
    print(f'published tree: {tree}')
    print(f'                {size} bytes in the whole checkout '
          '(not the reduction\'s figure, which counts only what it reduces)')

    reduction = container_of(state)
    work = state / 'verify'
    if work.exists():
        # Removed from inside a container, because the previous attempt made it
        # as root and this process is not. `shutil.rmtree(ignore_errors=True)`
        # was here and did what it was told: it failed, said nothing, and the
        # mkdir on the next line raised FileExistsError about a directory the
        # code believed it had just deleted. An error discarded is an error that
        # surfaces somewhere it cannot be understood.
        removed = run(['docker', 'run', '--rm', '-v', f'{state}:{state}',
                       IMAGE, 'rm', '-rf', str(work)])
        if removed.returncode != 0 or work.exists():
            print(f'cannot clear {work}: {removed.stderr.strip()[:200]}', file=sys.stderr)
            return 2
    (work / 'build').mkdir(parents=True)
    (work / 'ccache').mkdir(parents=True)

    if reduction:
        print(f'pausing the reduction ({reduction}) so this measures the tree, '
              'not the contention')
        run(['docker', 'pause', reduction])
    started = time.monotonic()
    try:
        # Configured and built from nothing, because an incremental build over
        # the reduction's own directory would answer about objects produced
        # before the deletions being checked.
        proc = run([
            'docker', 'run', '--rm',
            '-v', f'{repo}:{SRC}',
            '-v', f'{tree}:{SRC}/{SUBMODULE}',
            '-v', f'{work}:{work}',
            '-e', f'TMPDIR={work}',
            '-e', f'CCACHE_DIR={work}/ccache',
            '-e', 'CCACHE_MAXSIZE=20G',
            '-e', 'CCACHE_SLOPPINESS=pch_defines,time_macros',
            '-w', SRC, IMAGE,
            'sh', '-c',
            f'cmake --preset default -B {work}/build > {work}/configure.log 2>&1 && '
            f'cmake --build {work}/build --target ns_projection_unit_tests '
            f'> {work}/build.log 2>&1 && '
            f'ctest --test-dir {work}/build -R {test!r} --no-tests=error --output-on-failure',
        ])
    finally:
        if reduction:
            run(['docker', 'unpause', reduction])
            print(f'reduction resumed after {time.monotonic() - started:.0f} s')

    if proc.returncode == 0:
        print(f'VERIFIED: the published tree builds from nothing and {test} passes')
        # Immediately, rather than left to whoever is watching. The result is
        # known to be good exactly now, it lives in tmpfs, and the run that made
        # it may not survive the hour.
        preserve(tree, state, test)
        if not keep:
            # Through a container again: the build wrote as root, and a failure
            # to clean up must be visible rather than left for the next run to
            # trip over. It is the tmpfs the reduction itself lives in.
            gone = run(['docker', 'run', '--rm', '-v', f'{work.parent}:{work.parent}',
                        IMAGE, 'rm', '-rf', str(work)])
            if gone.returncode != 0 or work.exists():
                print(f'note: {work} could not be removed and still occupies the '
                      'tmpfs the reduction runs in', file=sys.stderr)
        return 0

    print(f'FAILED (exit {proc.returncode}): what the reduction has published does '
          'not build, or does not pass its own criterion')
    for name in ('configure.log', 'build.log'):
        log = work / name
        if log.exists():
            errors = [line for line in log.read_text(errors='replace').splitlines()
                      if 'error' in line.lower()]
            if errors:
                print(f'--- {name} ---')
                for line in errors[:8]:
                    print('   ', line[:160])
    if proc.stdout.strip():
        print('--- ctest ---')
        print(proc.stdout[-1500:])
    print(f'the whole attempt is left in {work}')
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('state', type=Path, help='the run state directory, /dev/shm/cvise-<name>')
    parser.add_argument('--repo', type=Path, default=Path('/dev/shm/ns-nvrtc'))
    parser.add_argument('--test', default='^IngestTest.ParsesRepresentativeRequestJson$')
    parser.add_argument('--keep', action='store_true',
                        help='keep the verification build even when it succeeded')
    args = parser.parse_args()
    return verify(args.state, args.test, args.repo, args.keep)


if __name__ == '__main__':
    sys.exit(main())
