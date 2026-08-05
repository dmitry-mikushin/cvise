#!/usr/bin/env python3
"""Does a file the candidate deleted stay deleted when something writes to it?

A build removes an output and makes it again; a tool truncates a log; a
generator rewrites a header. Every one of those is a write to a path that, for
this candidate, is not there. The write has to start from nothing, because the
content that used to be at that path belongs to a tree this candidate does not
have.

Getting it wrong is silent and it is not the write that shows it. MEASURED
before this was fixed -- delete a file, reopen it O_WRONLY|O_CREAT without
O_TRUNC, write three bytes:

    'NEWGINAL-CONTENT-THAT-THE-CANDIDATE-DELETED'

The deletion had been undone underneath by a copy-up of the shared original,
and the writer was extending content its own candidate never contained. The
symptom that led there was milder and easy to dismiss as an oddity of one tool:
O_CREAT|O_EXCL over a deleted path returned EEXIST, because the copy-up had
just put the file back. `cp` opens that way; a compiler does not, which is why
this survived a run that measured the compiler.

Every way of opening is checked, because which one a program uses was decided
by its author years ago and the overlay has already been caught serving one
spelling and not another.
"""
import errno
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ORIGINAL = 'ORIGINAL-CONTENT-THAT-THE-CANDIDATE-DELETED'

PROBE = r'''
import errno, os, sys

target = sys.argv[1]

def fresh():
    """Delete it again, so each way of opening starts from the same state."""
    try:
        os.unlink(target)
    except FileNotFoundError:
        pass

results = []

fresh()
fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o644)
os.write(fd, b'NEW')
os.close(fd)
results.append(('O_WRONLY|O_CREAT', open(target).read()))

fresh()
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
os.write(fd, b'NEW')
os.close(fd)
results.append(('O_WRONLY|O_CREAT|O_TRUNC', open(target).read()))

fresh()
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.write(fd, b'NEW')
os.close(fd)
results.append(("O_WRONLY|O_CREAT|O_APPEND", open(target).read()))

fresh()
try:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    os.write(fd, b'NEW')
    os.close(fd)
    results.append(('O_WRONLY|O_CREAT|O_EXCL', open(target).read()))
except OSError as e:
    results.append(('O_WRONLY|O_CREAT|O_EXCL',
                    'REFUSED ' + errno.errorcode.get(e.errno, str(e.errno))))

fresh()
with open(target, 'w') as f:
    f.write('NEW')
results.append(("builtin open(path, 'w')", open(target).read()))

for name, got in results:
    print(f'{name}\t{got}')
'''


def main():
    library = os.environ.get('CVISE_OVERLAY_LIB')
    if not library or not Path(library).exists():
        print('CVISE_OVERLAY_LIB is not set to an existing library', file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix='overlay-resurrect-'))
    root, delta = work / 'root', work / 'delta'
    root.mkdir()
    delta.mkdir()
    if delta.resolve() == root.resolve() or root.resolve() in delta.resolve().parents:
        print('  FAIL the delta is inside the root; this probe cannot tell a '
              'redirected write from a leaked one')
        return 2

    target = root / 'gone.txt'
    target.write_text(ORIGINAL)

    script = work / 'probe.py'
    script.write_text(PROBE)
    proc = subprocess.run(
        [sys.executable, str(script), str(target)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            'LD_PRELOAD': library,
            'CVISE_OVERLAY_DELTA': str(delta),
            'CVISE_OVERLAY_ROOT': str(root),
        },
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        print(f'INCONCLUSIVE: the probe did not run ({proc.stderr.strip()[:200]})')
        return 2

    ok = True
    for line in proc.stdout.splitlines():
        name, _, got = line.partition('\t')
        if got == 'NEW':
            print(f'  ok   {name}: wrote {got!r}, nothing underneath')
        elif got.startswith('REFUSED'):
            print(f'  FAIL {name}: {got} -- a path this candidate deleted must be '
                  'free to create')
            ok = False
        else:
            print(f'  FAIL {name}: wrote 3 bytes and read back {got!r} -- the '
                  'deletion was undone underneath')
            ok = False

    if target.read_text() != ORIGINAL:
        print('  FAIL the shared tree was modified; every other job reads it')
        ok = False
    else:
        print('  ok   the shared tree still holds the original')

    print('PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
