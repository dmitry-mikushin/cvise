#!/usr/bin/env python3
"""Can a job reach the shared inode through a file descriptor?

The overlay works on paths: it rewrites a name before the kernel sees it. A
descriptor has no name -- by the time a job holds one, the decision about which
file it refers to has already been made. If the job read a file it had not
written, that descriptor points at the pristine tree every other job is
compiling, and ftruncate, fchmod, fchown or futimens on it reach that inode.
Nothing in the call says which file it is, so nothing can redirect it.

What can be said for certain is which descriptors are safe: the ones opened for
writing, which the path layer already sent into the delta. A read-only
descriptor cannot point at a file this job owns, so a mutation through it is a
mutation of the shared tree.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LIB = os.environ.get('CVISE_OVERLAY_LIB', '')
if not LIB or not Path(LIB).exists():
    raise SystemExit(
        'set CVISE_OVERLAY_LIB to the built libcvise_overlay.so (ctest does this for you)'
    )


def under_overlay(code: str, delta: Path, root: Path):
    env = {
        **os.environ,
        'LD_PRELOAD': LIB,
        'CVISE_OVERLAY_DELTA': str(delta),
        'CVISE_OVERLAY_ROOT': str(root),
    }
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env)


def main():
    work = Path(tempfile.mkdtemp(prefix='overlay-fd-'))
    ok = True
    try:
        tree = work / 'tree'
        tree.mkdir()
        shared = tree / 'shared.txt'
        shared.write_text('0123456789\n')
        shared.chmod(0o644)
        delta = work / 'delta'
        delta.mkdir()

        size_before = shared.stat().st_size
        mode_before = shared.stat().st_mode & 0o777
        mtime_before = shared.stat().st_mtime_ns

        p = under_overlay(
            f'import os\n'
            f'fd = os.open({str(shared)!r}, os.O_RDONLY)\n'
            f'for label, call in (\n'
            f'    ("ftruncate", lambda: os.ftruncate(fd, 3)),\n'
            f'    ("fchmod", lambda: os.fchmod(fd, 0o600)),\n'
            f'    ("futimens", lambda: os.utime(fd, (1000, 1000))),\n'
            f'):\n'
            f'    try:\n'
            f'        call()\n'
            f'        print(label + ": ALLOWED")\n'
            f'    except OSError as e:\n'
            f'        print(label + ": refused")\n',
            delta, tree)
        for line in p.stdout.strip().splitlines():
            print(f'   {line}')

        checks = [
            ('the shared file kept its size', shared.stat().st_size == size_before),
            ('the shared file kept its mode', (shared.stat().st_mode & 0o777) == mode_before),
            ('the shared file kept its mtime', shared.stat().st_mtime_ns == mtime_before),
        ]
        print()
        for label, good in checks:
            print(f'{"ok  " if good else "FAIL"}  {label}')
            ok &= good

        # A descriptor the job opened for writing is its own, in the delta, and
        # must keep working -- refusing everything would be safe and useless.
        p = under_overlay(
            f'import os\n'
            f'fd = os.open({str(shared)!r}, os.O_WRONLY)\n'
            f'os.ftruncate(fd, 2)\n'
            f'print("own copy truncated")\n',
            delta, tree)
        own_ok = 'own copy truncated' in p.stdout
        print(f'{"ok  " if own_ok else "FAIL"}  a job can still truncate its own copy: '
              f'{p.stdout.strip() or p.stderr.strip()[:60]}')
        ok &= own_ok
        ok &= shared.stat().st_size == size_before

        print()
        print('THE SHARED INODE IS OUT OF REACH' if ok else 'A JOB CAN STILL REACH THE SHARED INODE')
        return 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
