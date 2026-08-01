#!/usr/bin/env python3
"""Can a job damage the tree that every other job is reading?

The overlay's whole promise is that the original stays pristine: dozens of jobs
read one shared source tree and one shared build directory, and each writes only
into its own delta. A single mutating call that is not intercepted breaks that
promise silently -- the job succeeds, the shared tree is altered, and every
other job is now compiling something nobody asked for.

The calls checked here all did exactly that until they were wrapped. They are
not exotic: rmdir and truncate are ordinary build-tool behaviour, and utime is
worse than either, because a build system decides what to rebuild from mtimes,
so one job restamping a shared source forces every other job to rebuild files
it never touched.
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


def under_overlay(code: str, delta: Path):
    env = {**os.environ, 'LD_PRELOAD': LIB, 'CVISE_OVERLAY_DELTA': str(delta)}
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env)


def main():
    work = Path(tempfile.mkdtemp(prefix='overlay-shared-'))
    ok = True
    try:
        tree = work / 'tree'
        (tree / 'doomed_dir').mkdir(parents=True)
        payload = tree / 'payload.txt'
        payload.write_text('0123456789abcdef\n')
        script = tree / 'run.sh'
        script.write_text('#!/bin/sh\nexit 0\n')
        script.chmod(0o755)

        before_size = payload.stat().st_size
        before_mtime = payload.stat().st_mtime_ns
        before_mode = script.stat().st_mode & 0o777

        delta = work / 'delta'
        delta.mkdir()
        p = under_overlay(
            f'import os\n'
            f'os.rmdir({str(tree / "doomed_dir")!r})\n'
            f'os.truncate({str(payload)!r}, 3)\n'
            f'os.utime({str(payload)!r}, (1000, 1000))\n'
            f'os.chmod({str(script)!r}, 0o600)\n',
            delta)
        if p.returncode != 0:
            print(p.stderr[-800:])

        checks = [
            ('rmdir left the shared directory alone', (tree / 'doomed_dir').is_dir()),
            ('truncate left the shared file alone', payload.stat().st_size == before_size),
            ('utime left the shared mtime alone', payload.stat().st_mtime_ns == before_mtime),
            ('chmod left the shared mode alone', (script.stat().st_mode & 0o777) == before_mode),
        ]
        for label, good in checks:
            print(f'{"ok  " if good else "FAIL"}  {label}')
            ok &= good

        print()
        print('THE SHARED TREE IS SAFE FROM A JOB' if ok else 'A JOB CAN STILL DAMAGE THE SHARED TREE')
        return 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
