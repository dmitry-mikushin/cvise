#!/usr/bin/env python3
"""Does deleting a file through the overlay actually hide it?

This is the case the overlay exists for and the one it silently got wrong: a
reduction that removes a file must make that file disappear for the build,
without touching the shared original that every other parallel job is reading.
Getting it wrong is invisible -- the build still succeeds, the file is still
there, and the run concludes the file was load-bearing when it was never
removed at all.

Four things are checked, each against a real process rather than by reading
code:

  1. a file that exists only in the original disappears after unlink;
  2. the original on disk is untouched;
  3. a second job with its own delta still sees the file;
  4. rename makes the old name disappear and the new name appear;
  5. the error for a hidden path is ENOENT, not ENOTDIR.
"""
import errno
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


def run(code: str, delta: Path | None):
    env = {**os.environ, 'LD_PRELOAD': LIB}
    if delta is not None:
        env['CVISE_OVERLAY_DELTA'] = str(delta)
    else:
        env.pop('CVISE_OVERLAY_DELTA', None)
    p = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env)
    return p.stdout.strip(), p.stderr.strip()


def main():
    work = Path(tempfile.mkdtemp(prefix='overlay-hide-'))
    ok = True
    try:
        original = work / 'tree' / 'doomed.txt'
        original.parent.mkdir(parents=True)
        original.write_text('load bearing?\n')
        keeper = work / 'tree' / 'keeper.txt'
        keeper.write_text('untouched\n')

        delta_a = work / 'delta-a'
        delta_a.mkdir()
        delta_b = work / 'delta-b'
        delta_b.mkdir()

        out, err = run(f'import os; os.unlink({str(original)!r}); print("unlinked")', delta_a)
        print(f'1. unlink through the overlay:        {out or err}')
        ok &= out == 'unlinked'

        out, _ = run(
            f'import os\n'
            f'print("visible" if os.path.exists({str(original)!r}) else "hidden")', delta_a)
        print(f'   the job now sees it as:            {out}')
        ok &= out == 'hidden'

        print(f'2. original still on disk:            {original.exists()} '
              f'({original.read_text().strip()!r})')
        ok &= original.exists()

        out, _ = run(
            f'import os\n'
            f'print("visible" if os.path.exists({str(original)!r}) else "hidden")', delta_b)
        print(f'3. a second job still sees it:        {out}')
        ok &= out == 'visible'

        renamed = work / 'tree' / 'moved.txt'
        out, err = run(
            f'import os\n'
            f'os.rename({str(keeper)!r}, {str(renamed)!r})\n'
            f'print(("old:" + ("visible" if os.path.exists({str(keeper)!r}) else "hidden")) + '
            f'      " new:" + ("visible" if os.path.exists({str(renamed)!r}) else "missing"))',
            delta_a)
        print(f'4. after rename:                      {out or err}')
        ok &= out == 'old:hidden new:visible'

        out, _ = run(
            f'import os, errno\n'
            f'try:\n'
            f'    open({str(original)!r})\n'
            f'    print("opened!")\n'
            f'except OSError as e:\n'
            f'    print(errno.errorcode[e.errno])', delta_a)
        print(f'5. errno for a hidden path:           {out}')
        ok &= out == 'ENOENT'

        print()
        print('DELETION THROUGH THE OVERLAY WORKS' if ok else 'DELETION IS STILL BROKEN')
        return 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
