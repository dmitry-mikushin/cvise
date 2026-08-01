#!/usr/bin/env python3
"""Three ways the overlay used to answer the wrong question.

Each of these was found by running the library rather than reading it, and each
is the kind of failure that does not announce itself: the call succeeds, the
job carries on, and the reduction draws a conclusion about code it never
actually tested.

  1. Listing a directory returned only what THIS job had written into it, so a
     source tree appeared to contain nothing but the object files just produced.
     It fired on the first write into a directory.
  2. Creating a symlink rewrote the link TARGET into the delta -- but a target
     is a string kept inside the link, not a path being opened, so every symlink
     the job made pointed at one job's private directory.
  3. Hard-linking a file that lived only in the shared tree failed with ENOENT,
     because the source of a link was sent down the path meant for callers that
     overwrite a file wholesale.

And one that was not subtle at all: the overlay redirected every absolute path
on the machine, including the runtime scratch of the tools themselves.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# CTest passes the built library; running the script by hand needs the path.
LIB = os.environ.get('CVISE_OVERLAY_LIB', '')
if not LIB or not Path(LIB).exists():
    raise SystemExit(
        'set CVISE_OVERLAY_LIB to the built libcvise_overlay.so '
        '(ctest does this for you)'
    )


def under_overlay(code: str, delta: Path, root: Path | None = None):
    env = {**os.environ, 'LD_PRELOAD': LIB, 'CVISE_OVERLAY_DELTA': str(delta)}
    if root is not None:
        env['CVISE_OVERLAY_ROOT'] = str(root)
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env)


def main():
    work = Path(tempfile.mkdtemp(prefix='overlay-answers-'))
    ok = True
    try:
        tree = work / 'tree'
        tree.mkdir()
        for n in ('a.cpp', 'b.cpp', 'c.cpp'):
            (tree / n).write_text(f'// {n}\n')
        delta = work / 'delta'
        delta.mkdir()

        p = under_overlay(
            f'import os\n'
            f'open({str(tree / "out.o")!r}, "w").write("obj")\n'
            f'print(" ".join(sorted(os.listdir({str(tree)!r}))))\n',
            delta, tree)
        listing = p.stdout.strip()
        good = all(n in listing for n in ('a.cpp', 'b.cpp', 'c.cpp'))
        print(f'{"ok  " if good else "FAIL"}  after writing into a directory it still lists the '
              f'shared files: {listing or p.stderr.strip()[:80]}')
        ok &= good

        target = tree / 'a.cpp'
        link = tree / 'link_to_a'
        p = under_overlay(
            f'import os; os.symlink({str(target)!r}, {str(link)!r})', delta, tree)
        stored = os.readlink(delta / str(link).lstrip('/')) if (delta / str(link).lstrip('/')).is_symlink() else ''
        good = stored == str(target)
        print(f'{"ok  " if good else "FAIL"}  a symlink stores the target it was given: '
              f'{stored or p.stderr.strip()[:80]}')
        ok &= good

        p = under_overlay(
            f'import os; os.link({str(tree / "b.cpp")!r}, {str(tree / "b_hard.cpp")!r}); print("linked")',
            delta, tree)
        good = p.stdout.strip() == 'linked'
        print(f'{"ok  " if good else "FAIL"}  hard-linking a file from the shared tree: '
              f'{p.stdout.strip() or p.stderr.strip()[:80]}')
        ok &= good

        outside = work / 'outside'
        outside.mkdir()
        p = under_overlay(
            f'import os\n'
            f'os.mkdir({str(outside / "runtime")!r})\n'
            f'print("real" if os.path.isdir({str(outside / "runtime")!r}) else "swallowed")\n',
            delta, tree)
        real = (outside / 'runtime').is_dir()
        print(f'{"ok  " if real else "FAIL"}  a path outside the reduction root is left alone: '
              f'{p.stdout.strip() or p.stderr.strip()[:80]}')
        ok &= real

        print()
        print('THE OVERLAY ANSWERS THESE CORRECTLY' if ok else 'THE OVERLAY STILL ANSWERS WRONG')
        return 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
