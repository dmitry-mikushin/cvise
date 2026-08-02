#!/usr/bin/env python3
"""Does the overlay isolate every tree it was given, and only those?

A reduction has two trees that must not be shared between jobs, not one. The
sources are the obvious one -- that is what a candidate changes. The build
directory is the other, and it is where the answer about the candidate is
computed: the objects, the link, and the build system's own record of what is
up to date. Left shared, it answers the next job with what the last one built,
because a file this candidate did not change is read from the pristine tree with
the pristine timestamp -- older than the object the previous candidate left, so
"up to date", so linked as it stands.

The roots are therefore a list. This probe checks that every root in it is
isolated, that the list is not read as a single name, and that a tree outside it
is still written through -- because redirecting everything is a different and
broken idea, not a safer version of this one.
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


def under_overlay(code: str, delta: Path, roots):
    env = {
        **os.environ,
        'LD_PRELOAD': LIB,
        'CVISE_OVERLAY_DELTA': str(delta),
        'CVISE_OVERLAY_ROOT': ':'.join(str(r) for r in roots),
    }
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env)


def main():
    work = Path(tempfile.mkdtemp(prefix='overlay-roots-'))
    ok = True
    try:
        sources = work / 'sources'
        build = work / 'build'
        elsewhere = work / 'elsewhere'
        for d in (sources, build, elsewhere):
            d.mkdir()
        for d in (sources, build, elsewhere):
            (d / 'file.txt').write_text('pristine\n')

        delta = work / 'delta'
        delta.mkdir()

        p = under_overlay(
            '\n'.join(
                f'open({str(d / "file.txt")!r}, "w").write("job wrote here\\n")'
                for d in (sources, build, elsewhere)
            ),
            delta,
            [sources, build],
        )
        if p.returncode != 0:
            print(p.stderr[-800:])

        checks = [
            ('the first root is isolated', (sources / 'file.txt').read_text() == 'pristine\n'),
            ('the second root is isolated too', (build / 'file.txt').read_text() == 'pristine\n'),
            (
                "a tree that is not a root is not touched by the overlay",
                (elsewhere / 'file.txt').read_text() == 'job wrote here\n',
            ),
            (
                'the first root received the write in the delta',
                (delta / str(sources / 'file.txt').lstrip('/')).is_file(),
            ),
            (
                'the second root received the write in the delta',
                (delta / str(build / 'file.txt').lstrip('/')).is_file(),
            ),
        ]

        # A single root must keep working exactly as before: the list is a
        # generalisation, not a new spelling that breaks the old one.
        single_delta = work / 'single'
        single_delta.mkdir()
        (sources / 'single.txt').write_text('pristine\n')
        under_overlay(
            f'open({str(sources / "single.txt")!r}, "w").write("job\\n")',
            single_delta,
            [sources],
        )
        checks.append(
            ('one root on its own still isolates', (sources / 'single.txt').read_text() == 'pristine\n')
        )

        # The prefix test is on path components, not on characters: a directory
        # whose name merely starts with a root's name is a different directory.
        sibling = Path(str(sources) + '-other')
        sibling.mkdir()
        (sibling / 'file.txt').write_text('pristine\n')
        sibling_delta = work / 'sibling'
        sibling_delta.mkdir()
        under_overlay(
            f'open({str(sibling / "file.txt")!r}, "w").write("job wrote here\\n")',
            sibling_delta,
            [sources, build],
        )
        checks.append(
            (
                'a directory whose name only starts like a root is not a root',
                (sibling / 'file.txt').read_text() == 'job wrote here\n',
            )
        )

        for label, good in checks:
            print(f'{"ok  " if good else "FAIL"}  {label}')
            ok &= good

        print()
        print('EVERY TREE UNDER REDUCTION IS ISOLATED' if ok else 'A TREE UNDER REDUCTION IS STILL SHARED')
        return 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
