#!/usr/bin/env python3
"""Can a job reach the shared inode by mutating through a file descriptor?

The overlay is path-based: it rewrites a pathname before the kernel sees it.
That is enough for the calls that name what they touch -- open, rmdir, chmod,
truncate -- and the other probes show those land in the delta. It is not enough
for the calls that carry no path at all, only a file descriptor: futimens,
fchmod, fchown. A read of a shared file that has no delta copy is served from
the shared inode directly, so the descriptor the job holds IS the shared inode,
and a time-stamp written through it lands on the original -- the path overlay
never sees it.

A read-only bind of the shared tree closes that hole. Inside a namespace where
the tree is mounted read-only, the descriptor the job holds is still the shared
inode, but the kernel will not let it be written: the mutation that slipped past
the overlay meets a read-only filesystem and dies there, never reaching the
shared inode. bwrap builds that namespace from an unprivileged user namespace,
so the foundation needs no SYS_ADMIN, no container, no privileges a developer
does not already have.

What this probe proves, in order:

  1. the leak is real: with the overlay alone and the tree read-write, an
     open(O_RDONLY) followed by futimens on the fd DOES restamp the shared file.
     If this did not change the shared mtime, the whole probe would be testing
     nothing.
  2. the foundation holds: the SAME job inside a bwrap read-only bind of the
     tree cannot reach the shared inode -- the fd mutation fails (the kernel
     refuses it) and the shared mtime is left exactly as it was.
  3. the foundation does not starve the overlay: a plain open(w) of a shared
     file still copies up into the delta and the shared content stays pristine.
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

# A stamp far from anything the filesystem would produce on its own, so the
# check is "did the shared mtime become this value" rather than a fuzzy diff.
LEAK_STAMP = 1_000_000_000
SAFE_STAMP = 1_500_000_000

# bwrap needs the runtime it is about to run in. These are read-only binds of
# the host machinery that lets /usr/bin/python3 start; none of them reach the
# shared tree, which is bound separately and is the only thing whose
# read-onliness this probe cares about.
BWRAP_BASE = [
    '--ro-bind', '/usr', '/usr',
    '--ro-bind', '/lib', '/lib',
    '--ro-bind', '/lib64', '/lib64',
    '--dev', '/dev',
    '--tmpfs', '/tmp',
    '--proc', '/proc',
]


def mutate_script():
    """open a file read-only and restamp it through the descriptor."""
    return (
        'import os, sys\n'
        'p = os.environ["CVISE_OVERLAY_ROOT"] + "/payload.txt"\n'
        'try:\n'
        '    fd = os.open(p, os.O_RDONLY)\n'
        f'    os.utime(fd, ({LEAK_STAMP}, {LEAK_STAMP}))\n'
        '    os.close(fd)\n'
        '    print("restamped"); sys.exit(0)\n'
        'except OSError as e:\n'
        '    print("refused: " + str(e)); sys.exit(2)\n'
    )


def copyup_script():
    """overwrite the shared file the normal way, which the overlay redirects."""
    return (
        'import os\n'
        'p = os.environ["CVISE_OVERLAY_ROOT"] + "/payload.txt"\n'
        'open(p, "w").write("DELTA\\n")\n'
    )


def write_script(dst: Path, code: str):
    dst.write_text(code)
    dst.chmod(0o755)


def bwrap_run(tree: Path, delta: Path, script: Path, shared_ro: bool):
    """Run a script under the overlay inside a bwrap namespace.

    The shared tree is bound read-only when shared_ro is set (the foundation),
    and read-write otherwise (the negative control). The delta is always bound
    read-write so the overlay can copy up into it. The tree path is bound to
    itself, so the absolute path the child sees is identical to the host's and
    no path rewriting is needed.
    """
    tree_mount = '--ro-bind' if shared_ro else '--bind'
    cmd = ['bwrap', *BWRAP_BASE,
           tree_mount, str(tree), str(tree),
           '--bind', str(delta), str(delta),
           '--ro-bind', str(script), str(script),
           '--ro-bind', LIB, LIB,
           '--setenv', 'LD_PRELOAD', LIB,
           '--setenv', 'CVISE_OVERLAY_ROOT', str(tree),
           '--setenv', 'CVISE_OVERLAY_DELTA', str(delta),
           '/usr/bin/python3', str(script)]
    return subprocess.run(cmd, capture_output=True, text=True)


def main():
    work = Path(tempfile.mkdtemp(prefix='overlay-rofs-'))
    ok = True
    try:
        tree = work / 'tree'
        payload = tree / 'payload.txt'
        payload.parent.mkdir(parents=True)
        payload.write_text('SHARED\n')
        delta = work / 'delta'
        delta.mkdir()
        script = work / 'mutate.py'
        copyup = work / 'copyup.py'
        write_script(script, mutate_script())
        write_script(copyup, copyup_script())

        # ---- 1. the negative control: the overlay alone cannot stop a
        # descriptor mutation, so the shared inode IS changed. This is the
        # proof that the leak is real and that the probe exercises it.
        os.utime(payload, (SAFE_STAMP, SAFE_STAMP))
        baseline = payload.stat().st_mtime_ns
        p = bwrap_run(tree, delta, script, shared_ro=False)
        leaked = payload.stat().st_mtime_ns != baseline
        print(f'{"ok  " if leaked else "FAIL"}  without the foundation, fd-utime '
              f'reaches the shared inode: {p.stdout.strip()}')
        if not leaked:
            print(p.stderr[-800:])
        ok &= leaked

        # ---- 2. the foundation: the same job in a read-only bind of the tree.
        # The descriptor still points at the shared inode, but the kernel
        # refuses the write, so the shared mtime is left untouched.
        os.utime(payload, (SAFE_STAMP, SAFE_STAMP))
        baseline = payload.stat().st_mtime_ns
        p = bwrap_run(tree, delta, script, shared_ro=True)
        protected = payload.stat().st_mtime_ns == baseline
        print(f'{"ok  " if protected else "FAIL"}  under the read-only foundation, '
              f'the shared inode is protected: {p.stdout.strip()}')
        if not protected:
            print(p.stderr[-800:])
        ok &= protected

        # ---- 3. the foundation does not starve the overlay: a normal write
        # still copies up into the delta and the shared content stays pristine.
        p = bwrap_run(tree, delta, copyup, shared_ro=True)
        shared_still_shared = payload.read_text() == 'SHARED\n'
        in_delta = (delta / str(payload).lstrip('/'))
        landed = in_delta.read_text() == 'DELTA\n' if in_delta.exists() else False
        print(f'{"ok  " if landed and shared_still_shared else "FAIL"}  '
              f'a write still copies up under the foundation: '
              f'delta={"DELTA" if landed else "missing"}, '
              f'shared={"SHARED" if shared_still_shared else payload.read_text().strip()}')
        ok &= landed and shared_still_shared

        print()
        print('THE READ-ONLY FOUNDATION MAKES FD LEAKS FAIL-SAFE'
              if ok else 'A FD MUTATION CAN STILL REACH THE SHARED INODE')
        return 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
