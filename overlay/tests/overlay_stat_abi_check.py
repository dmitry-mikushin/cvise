#!/usr/bin/env python3
"""Is a hidden file hidden from EVERY way of asking?

A program asks "is this file there" through whichever of a dozen libc entry
points its compiler chose years ago, and they are not interchangeable. glibc
2.33 stopped declaring the `__xstat` family and kept exporting it forever, so a
binary built before that change calls symbols a modern header will not mention.
An overlay that decides what to interpose by asking its own build machine's
headers compiles those wrappers out, and every such program reads straight
through it.

That is not a corner case, it is what happened. The ninja in one project's
build image imports `__xstat64` and `__fxstat64`. On one path, in one process,
at one instant:

    stat()      -> ENOENT        <- python, clang, every probe written by hand
    __xstat()   -> PRESENT       <- ninja
    __xstat64() -> PRESENT

so a candidate that had 376 object files taken away from it was told there was
nothing to rebuild, its test ran the previous candidate's binary, and it passed.
Two full runs ended with no verdict at all, and the overlay reported itself
healthy throughout, because the only question being asked was whether the
library had loaded.

The two directions are both checked, because they fail separately: a file that
exists only in the delta must be visible, and a file deleted by this job must be
invisible, whichever entry point is used.
"""
import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path


AT_FDCWD = -100

# How each spelling is CALLED -- a property of the C library's signatures, not
# of what this build wraps. WHICH of them are exercised is read off the library
# below, so that a wrapper compiled out takes its entry here with it and a
# wrapper added is exercised without anyone remembering to add it here too. Two
# hand-kept lists agreeing is a coincidence renewed at every edit, and when they
# stopped agreeing nothing said so.
HOW_TO_ASK = {
    'stat': 'fn(path, buf)',
    'stat64': 'fn(path, buf)',
    'lstat': 'fn(path, buf)',
    'lstat64': 'fn(path, buf)',
    'fstatat': f'fn({AT_FDCWD}, path, buf, 0)',
    'fstatat64': f'fn({AT_FDCWD}, path, buf, 0)',
    '__xstat': 'fn(1, path, buf)',
    '__xstat64': 'fn(1, path, buf)',
    '__lxstat': 'fn(1, path, buf)',
    '__lxstat64': 'fn(1, path, buf)',
    '__fxstatat': f'fn(1, {AT_FDCWD}, path, buf, 0)',
    '__fxstatat64': f'fn(1, {AT_FDCWD}, path, buf, 0)',
}

# The least this may ever come down to. Reading the list off the library makes
# this follow the library downwards as well: one that wrapped nothing would be
# checked for nothing and would pass.
REQUIRED = ('stat', '__xstat64')

ASK = '''
import ctypes
libc = ctypes.CDLL(None)
buf = ctypes.create_string_buffer(1024)
path = {path!r}
for name in {names!r}:
    try:
        fn = getattr(libc, name)
    except AttributeError:
        continue
    print(name, eval({calls!r}[name]))
'''


def wrapped_by(library):
    """Which spellings this library actually wraps."""
    out = subprocess.run(['nm', '-D', '--defined-only', str(library)],
                         capture_output=True, text=True).stdout
    names = {line.split()[-1] for line in out.splitlines() if ' T ' in line}
    return {name: how for name, how in HOW_TO_ASK.items() if name in names}


def ask(library, path, delta, root, entry_points):
    """What every entry point this library wraps says about a path, inside a job."""
    code = ASK.format(path=str(path).encode(), names=sorted(entry_points), calls=entry_points)
    proc = subprocess.run(
        [sys.executable, '-c', code],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            'LD_PRELOAD': library,
            'CVISE_OVERLAY_DELTA': str(delta),
            'CVISE_OVERLAY_ROOT': str(root),
        },
    )
    if proc.returncode != 0:
        print('the probe could not run:', proc.stderr.strip()[:300], file=sys.stderr)
        return None
    seen = {}
    for line in proc.stdout.splitlines():
        name, _, value = line.partition(' ')
        seen[name] = int(value) == 0
    return seen


def report(what, seen, expected):
    """expected: True if every entry point must find the file."""
    wrong = sorted(name for name, found in seen.items() if found is not expected)
    word = 'find' if expected else 'not find'
    if wrong:
        print(f'  FAIL {what}: {len(seen) - len(wrong)} of {len(seen)} entry points '
              f'{word} it; these do not: {", ".join(wrong)}')
        return False
    print(f'  ok   {what}: all {len(seen)} entry points agree')
    return True


def main():
    library = os.environ.get('CVISE_OVERLAY_LIB')
    if not library or not Path(library).exists():
        print('CVISE_OVERLAY_LIB is not set to an existing library', file=sys.stderr)
        return 2

    entry_points = wrapped_by(library)
    absent = [name for name in REQUIRED if name not in entry_points]
    if absent:
        print(f'  FAIL the library does not wrap {", ".join(absent)}, so a build tool '
              f'that spells the question that way reads the original tree')
        print('FAIL')
        return 1
    print(f'  ok   the library wraps {len(entry_points)} of '
          f'{len(HOW_TO_ASK)} known spellings')

    work = Path(tempfile.mkdtemp(prefix='overlay-stat-abi-'))
    root, delta = work / 'root', work / 'delta'
    root.mkdir()
    delta.mkdir()

    shared = root / 'baseline.o'
    shared.write_text('an object the baseline build produced')

    # The arrangement has to be able to give a wrong answer. With the delta
    # underneath the root, a write that correctly went to the delta and a write
    # that leaked into the shared tree are the same write; with the root at `/`,
    # every path is inside it and "did this stay out of the shared tree" means
    # nothing. Both have already produced one confident report of a leak that
    # was not there.
    if delta.resolve() == root.resolve() or root.resolve() in delta.resolve().parents:
        print(f'  FAIL the delta {delta} is inside the root {root}; this probe '
              'cannot tell a redirected write from a leaked one')
        return 2
    if str(root.resolve()) == '/':
        print('  FAIL the root is the filesystem root, so nothing is outside it')
        return 2

    ok = True

    # A file this job deleted. This is the ninja case: the build system asks
    # whether its output is still there, and an entry point that answers "yes"
    # makes it rebuild nothing.
    subprocess.run(
        [sys.executable, '-c', f'import os; os.unlink({str(shared)!r})'],
        env={**os.environ, 'LD_PRELOAD': library,
             'CVISE_OVERLAY_DELTA': str(delta), 'CVISE_OVERLAY_ROOT': str(root)},
        check=True,
    )
    seen = ask(library, shared, delta, root, entry_points)
    if seen is None:
        return 2
    ok &= report('a file this job deleted', seen, expected=False)

    if not shared.exists():
        print('  FAIL the shared tree lost the file; other jobs read that tree')
        ok = False
    else:
        print('  ok   the shared tree still has it')

    # A file that exists only in this job's delta, which is how a candidate's
    # own version of a source reaches the compiler.
    only = root / 'candidate.cpp'
    (delta / str(only).lstrip('/')).parent.mkdir(parents=True, exist_ok=True)
    (delta / str(only).lstrip('/')).write_text('int main() { return 0; }\n')
    seen = ask(library, only, delta, root, entry_points)
    if seen is None:
        return 2
    ok &= report('a file only this job has', seen, expected=True)

    # At least the legacy family has to be among what was tested. If a future
    # change compiles those wrappers out again, every check above still passes
    # -- on the entry points that remain -- and says nothing about the ones that
    # went missing. That silence is the whole failure being guarded against.
    legacy = {name for name in seen if name.startswith('__')}
    if not legacy:
        print('  FAIL no legacy __xstat entry point was exercised at all, so a build '
              'tool that uses one is untested')
        ok = False
    else:
        print(f'  ok   the legacy family was exercised: {", ".join(sorted(legacy))}')

    print('PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
