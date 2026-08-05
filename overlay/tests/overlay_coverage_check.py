#!/usr/bin/env python3
"""Does every way a real binary asks for a path go through the overlay?

The overlay redirects paths. Which libc entry points it wraps was a list
maintained by hand, and the list of entry points a checker exercised was a
second list maintained by hand, so the two could disagree with nobody noticing
-- and they did. The overlay wrapped `stat` and not `__xstat64`; the checker
asked about `stat` and not `__xstat64`; the ninja in one project's build image
calls `__xstat64`. It read the original tree, rebuilt nothing, and two runs
ended with every candidate graded on the previous candidate's binary.

So neither list is written here. Both are read off the artefacts:

  * what the overlay wraps comes from the library's own exported symbols;
  * what a program asks for comes from that program's undefined symbols.

and a variant is required whenever its plain form is wrapped. C libraries spell
the same operation many ways -- `open`, `open64`, `__open`, `__open_2`,
`__open64_2` -- and which one a binary calls was decided by the headers it was
compiled against years ago. Wrapping one and not the others is a hole shaped
exactly like the program that happens to use the other spelling.

Entry points that take a file descriptor rather than a path are not variants of
anything wrapped, and are not required: `__fxstat` is `fstat` on an already-open
descriptor, and the redirection happened when the descriptor was opened.
MEASURED -- a file shadowed by the delta, opened through the overlay:

    fstat(fd)      size 52     (the delta's version)
    __fxstat(fd)   size 52
    __fxstat64(fd) size 52

with the original 18 bytes long. Upstream fakechroot wraps neither, for the same
reason.
"""
import os
import subprocess
import sys
from pathlib import Path


def exported(path):
    out = subprocess.run(['nm', '-D', '--defined-only', str(path)],
                         capture_output=True, text=True).stdout
    return {line.split()[-1] for line in out.splitlines() if ' T ' in line}


def imported(path):
    out = subprocess.run(['nm', '-D', str(path)], capture_output=True, text=True).stdout
    return {line.split()[-1].split('@')[0] for line in out.splitlines() if ' U ' in line}


def plain(name):
    """The operation a spelling denotes, with the decoration taken off.

    __open_2, open64 and __open64_2 are all `open`; __xstat64 and stat64 are
    both `stat`; __lxstat is `lstat`; __fxstatat64 is `fstatat`. The x is
    glibc's marker for the versioned form that takes a leading `ver` argument,
    and it sits where the operation's first letter would be.
    """
    name = name.removeprefix('__')
    name = name.removesuffix('_chk')
    name = name.removesuffix('_2')
    name = name.removesuffix('64')
    # xstat -> stat, lxstat -> lstat, fxstatat -> fstatat
    if 'xstat' in name:
        name = name.replace('xstat', 'stat', 1)
    return name


def main():
    library = os.environ.get('CVISE_OVERLAY_LIB')
    if not library or not Path(library).exists():
        print('CVISE_OVERLAY_LIB is not set to an existing library', file=sys.stderr)
        return 2

    wrapped = exported(library)
    # What the overlay claims to handle at all, as operations rather than as
    # spellings. Anything outside this is not its business.
    handled = {plain(name) for name in wrapped}

    binaries = sys.argv[1:] or [
        path for path in (
            '/usr/bin/ninja', '/usr/bin/cmake', '/usr/bin/ctest', '/usr/bin/make',
            '/usr/bin/ccache', '/usr/bin/ld', '/usr/bin/ar',
        ) if Path(path).exists()
    ]
    if not binaries:
        print('no binaries to examine', file=sys.stderr)
        return 2

    print(f'the overlay wraps {len(wrapped)} entry points, '
          f'covering {len(handled)} operations')
    ok = True
    for binary in binaries:
        holes = sorted(
            name for name in imported(binary)
            if plain(name) in handled and name not in wrapped
        )
        through = sorted(name for name in imported(binary) if name in wrapped)
        if holes:
            ok = False
            print(f'  FAIL {binary}')
            print(f'       asks through the overlay: {", ".join(through) or "nothing"}')
            print(f'       AROUND it: {", ".join(holes)}')
        else:
            print(f'  ok   {binary}: {len(through)} entry points, all wrapped')

    print('PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
