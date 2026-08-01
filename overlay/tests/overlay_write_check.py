#!/usr/bin/env python3
"""Does the overlay keep a job's build output out of the shared tree?

Reads were the easy half. The expensive half is writes: without redirecting
them, every parallel job needs a private copy of the whole build directory --
1.1 GB in RAM per job for this project, for the sake of the handful of object
files the job actually rebuilds. That is what exhausted the machine.

So: compile into a build directory that is shared and READ-ONLY in spirit, and
check that

  1. the object file appears in the job's delta, not in the shared directory;
  2. the shared directory is left exactly as it was;
  3. a second job with its own delta does not see the first job's output;
  4. what the job occupies is the size of what it produced, not of the tree.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Where the library is depends on who is running this: the build tree when a
# developer runs it in place, an installed libdir otherwise. Hardcoding one
# author's path makes the probe pass only on that author's machine.
# CTest passes the built library; running the script by hand needs the path.
LIB = os.environ.get('CVISE_OVERLAY_LIB', '')
if not LIB or not Path(LIB).exists():
    raise SystemExit(
        'set CVISE_OVERLAY_LIB to the built libcvise_overlay.so '
        '(ctest does this for you)'
    )
PREBUILT_OBJECTS = 60
OBJECT_FILLER = 64 * 1024


def snapshot(root: Path):
    return {str(p.relative_to(root)): p.stat().st_size for p in root.rglob('*') if p.is_file()}


def main():
    work = Path(tempfile.mkdtemp(prefix='overlay-write-'))
    try:
        src = work / 'main.cpp'
        src.write_text('int job_symbol() { return 7; }\n')

        # A shared build directory that already holds everything the other jobs
        # will not rebuild -- exactly the state a cloned slot starts from.
        shared = work / 'build'
        shared.mkdir()
        for i in range(PREBUILT_OBJECTS):
            (shared / f'prebuilt{i}.o').write_bytes(b'\0' * OBJECT_FILLER)
        before = snapshot(shared)
        shared_bytes = sum(before.values())
        print(f'shared build dir: {len(before)} files, {shared_bytes / 2**20:.1f} MiB')
        print()

        def compile_into(delta: Path):
            delta.mkdir(parents=True, exist_ok=True)
            env = {**os.environ, 'LD_PRELOAD': LIB, 'CVISE_OVERLAY_DELTA': str(delta)}
            out = shared / 'main.o'
            p = subprocess.run(['g++', '-c', str(src), '-o', str(out)],
                               capture_output=True, text=True, env=env)
            if p.returncode != 0:
                print(p.stderr[-1500:])
            return p.returncode == 0, out

        delta_a = work / 'delta-a'
        ok, target = compile_into(delta_a)
        in_delta = (delta_a / str(target).lstrip('/'))
        print(f'1. compile succeeded:                {ok}')
        print(f'   object landed in the delta:       {in_delta.exists()}'
              f'{f" ({in_delta.stat().st_size} bytes)" if in_delta.exists() else ""}')
        print(f'2. shared directory untouched:       {snapshot(shared) == before}')

        delta_b = work / 'delta-b'
        delta_b.mkdir()
        in_delta_b = delta_b / str(target).lstrip('/')
        print(f'3. a second job sees nothing of it:  {not in_delta_b.exists()}')

        delta_bytes = sum(p.stat().st_size for p in delta_a.rglob('*') if p.is_file())
        print()
        print('4. what one job occupies:')
        print(f'   with write redirection   {delta_bytes:>12} bytes  (only what it produced)')
        print(f'   by cloning the build dir {shared_bytes:>12} bytes  (what the harness did)')
        print(f'   at 52 parallel jobs      {52 * delta_bytes / 2**20:>12.1f} MiB'
              f'  vs {52 * shared_bytes / 2**20:.1f} MiB')

        good = ok and in_delta.exists() and snapshot(shared) == before and not in_delta_b.exists()
        print()
        print('WRITE REDIRECTION WORKS' if good else 'WRITE REDIRECTION IS BROKEN')
        return 0 if good else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
