#!/usr/bin/env python3
"""Where does a job's new directory go, and where is it standing afterwards?

Two questions that the proof does not answer and that a build asks constantly.
A build system creates output directories and then works inside them, and if
either half escapes the overlay the job is operating on the shared tree: the
directory appears where every other job can see it, or the job's own relative
paths resolve against a place the redirection never hears about.

The activation is the part that has been got wrong repeatedly, so it is set up
here to make a wrong answer impossible to mistake for a right one:

  * the root is a real directory, NOT the filesystem root. With root=/ every
    path is inside the root and "did it stay out of the root" has no meaning.
  * the delta is OUTSIDE the root. With the delta inside the root, a write that
    correctly went to the delta and a write that leaked into the root are the
    same write, and the probe cannot tell them apart.
  * every question is asked twice -- once by a process inside the job, once by
    this process outside it -- because the whole failure mode is that those two
    disagree and only the outside one is the truth about the shared tree.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


INSIDE = r'''
import os, sys
root = sys.argv[1]

# The common case first, and it is the one that must not change: a directory
# that exists in the shared tree. Everything else in this library assumes the
# working directory is a shared path -- rel2abs resolves against the real cwd
# and the result is substituted afterwards -- so moving the cwd into the delta
# here would put a delta prefix on every relative name the build uses and on
# every path a compiler writes into a depfile from it.
shared_dir = os.path.join(root, 'shared')
os.chdir(shared_dir)
print('cwd in shared    ', os.getcwd())
print('canonical        ', os.getcwd() == shared_dir)
with open('from_shared.o', 'w') as f:
    f.write('built while standing in the shared directory')

new = os.path.join(root, 'built')

os.mkdir(new)
print('mkdir            ok')
print('exists after     ', os.path.isdir(new))

os.chdir(new)
print('chdir            ok')
print('getcwd           ', os.getcwd())

# A relative name, which is the whole reason the working directory matters.
with open('output.o', 'w') as f:
    f.write('an object this job produced')
print('relative write   ok')
print('relative read    ', open('output.o').read())
print('absolute read    ', open(os.path.join(new, 'output.o')).read())
'''


def main():
    library = os.environ.get('CVISE_OVERLAY_LIB')
    if not library or not Path(library).exists():
        print('CVISE_OVERLAY_LIB is not set to an existing library', file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix='overlay-cwd-'))
    root = work / 'root'
    delta = work / 'delta'      # deliberately a sibling of the root, not inside it
    root.mkdir()
    delta.mkdir()
    (root / 'existing.txt').write_text('from the shared tree')
    (root / 'shared').mkdir()   # a directory the baseline build already made

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

    print(f'root  {root}')
    print(f'delta {delta}   (outside the root, so a leak cannot look like a success)')
    print()

    proc = subprocess.run(
        [sys.executable, '-c', INSIDE, str(root)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            'LD_PRELOAD': library,
            'CVISE_OVERLAY_DELTA': str(delta),
            'CVISE_OVERLAY_ROOT': str(root),
        },
    )
    print('--- inside the job ---')
    print(proc.stdout, end='')
    if proc.stderr.strip():
        print('stderr:', proc.stderr.strip()[:400])
    if proc.returncode != 0:
        print()
        print('INCONCLUSIVE: the probe did not finish, so nothing below is a measurement')
        return 2

    print()
    print('--- from outside, which is the truth about the shared tree ---')
    leaked_dir = (root / 'built').exists()
    leaked_file = (root / 'built' / 'output.o').exists()
    in_delta_dir = (delta / str(root).lstrip('/') / 'built').exists()
    in_delta_file = (delta / str(root).lstrip('/') / 'built' / 'output.o').exists()
    print(f'directory in the shared tree : {leaked_dir}')
    print(f'file      in the shared tree : {leaked_file}')
    print(f'directory in the job delta   : {in_delta_dir}')
    print(f'file      in the job delta   : {in_delta_file}')
    print(f'shared tree now contains     : {sorted(p.name for p in root.iterdir())}')

    canonical = [line for line in proc.stdout.splitlines() if line.startswith('canonical')]
    kept = canonical and canonical[0].split()[-1] == 'True'
    from_shared_leaked = (root / 'shared' / 'from_shared.o').exists()
    from_shared_in_delta = (delta / str(root).lstrip('/') / 'shared' / 'from_shared.o').exists()
    print(f'cwd stayed canonical for a shared directory: {bool(kept)}')
    print(f'output from there in the shared tree       : {from_shared_leaked}')
    print(f'output from there in the delta             : {from_shared_in_delta}')

    ok = True
    if not kept:
        print()
        print('the working directory moved into the delta for a directory that exists in')
        print('the shared tree, so every relative name from there carries a delta prefix')
        ok = False
    if from_shared_leaked or not from_shared_in_delta:
        print()
        print('a build standing in a shared directory did not write into its delta')
        ok = False
    if leaked_dir or leaked_file:
        print()
        print('LEAK: the job created something in the tree every other job reads.')
        ok = False
    if not in_delta_dir or not in_delta_file:
        print()
        print('NOT IN THE DELTA: the job produced it somewhere neither expected.')
        ok = False

    # Where the job thought it was standing. If chdir is not redirected the cwd
    # is the shared path, and every relative name the build uses afterwards is
    # resolved from there -- correctly, only as long as the redirection catches
    # the resolved path. The measurement above already says whether it did.
    reported = [line for line in proc.stdout.splitlines() if line.startswith('getcwd')]
    print()
    print('cwd as the job saw it:', reported[0].split(None, 1)[1].strip() if reported else '?')
    print('(the cwd being the shared path is not itself a leak -- what settles it is')
    print(' where the relative write landed, which is measured above)')

    print()
    print('PASS' if ok else 'FAIL')
    shutil.rmtree(work, ignore_errors=True)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
