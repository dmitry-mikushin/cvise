#!/usr/bin/env python3
"""Does a job's linked output escape the overlay into the shared tree?

Compiling an object was the easy half (overlay_write_check.py). Linking is the
half that broke: the linker writes its output through the large-file
fopen64/open64 path, and a shared build directory that is read-only in spirit
must never receive the resulting ELF -- otherwise every parallel job that reads
that path sees one job's private binary, and the shared inode is mutated.

So: link a trivial program into a shared output path, and check that

  1. the link succeeds and the ELF appears in the job's delta, not shared;
  2. the shared directory is left byte-for-byte as it was;
  3. the shared path was never created at all;
  4. what landed in the delta is a real ELF.

This guards the whole open64/fopen64/openat64/creat64 family at once: if any of
those write paths stops being intercepted, the ELF turns up in the shared tree
and steps 1-3 fail loudly.
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

ELF_MAGIC = b'\x7fELF'


def snapshot(root: Path):
    return {str(p.relative_to(root)): p.stat().st_size for p in root.rglob('*') if p.is_file()}


def main():
    work = Path(tempfile.mkdtemp(prefix='overlay-link-'))
    try:
        src = work / 'prog.c'
        src.write_text('int main(void) { return 0; }\n')

        # A shared build directory, read-only in spirit, exactly the state a
        # cloned slot starts from.
        shared = work / 'build'
        shared.mkdir()
        out = shared / 'prog'
        before = snapshot(shared)
        print(f'shared build dir: {len(before)} files')
        print()

        delta = work / 'delta'
        delta.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, 'LD_PRELOAD': LIB, 'CVISE_OVERLAY_DELTA': str(delta)}
        p = subprocess.run(['cc', str(src), '-o', str(out)],
                           capture_output=True, text=True, env=env)
        ok = p.returncode == 0
        if not ok:
            print(p.stderr[-1500:])

        in_delta = delta / str(out).lstrip('/')
        delta_bytes = in_delta.stat().st_size if in_delta.exists() else 0
        is_elf = in_delta.exists() and in_delta.read_bytes()[:4] == ELF_MAGIC

        print(f'1. link succeeded:                   {ok}')
        print(f'   ELF landed in the delta:          {in_delta.exists()}'
              f'{f" ({delta_bytes} bytes)" if in_delta.exists() else ""}')
        print(f'   shared directory untouched:       {snapshot(shared) == before}')
        print(f'   shared path was never created:    {not out.exists()}')
        print(f'   delta output is a real ELF:       {is_elf}')
        print()

        good = ok and in_delta.exists() and snapshot(shared) == before \
            and not out.exists() and is_elf
        print('LINK OUTPUT STAYS IN THE DELTA' if good else 'LINK OUTPUT LEAKS INTO THE SHARED TREE')
        return 0 if good else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
