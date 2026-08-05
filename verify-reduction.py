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


def verify(state: Path, test: str, repo: Path, keep: bool) -> int:
    tree = state / 'ns-projection'
    if not tree.is_dir():
        print(f'no published tree at {tree}', file=sys.stderr)
        return 2

    size = sum(f.stat().st_size for f in tree.rglob('*') if f.is_file())
    print(f'published tree: {tree}')
    print(f'                {size} bytes')

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
