#!/usr/bin/env python3
"""Does the overlay actually feed the compiler the variant, without copying?

The harness that killed the machine did not use the overlay at all: it rsync'ed
a private copy of the source tree for every job and cloned a build directory for
every job, all of it in RAM. The overlay exists precisely so that a variant
costs one file instead of one tree. Before relying on that, it has to be shown
that a real compiler -- not a toy -- reads the delta through the original path.

Three things are checked:

  1. correctness: g++ compiling the ORIGINAL path produces the symbol that only
     exists in the delta version, and the original file on disk is untouched;
  2. isolation: with the delta unset, the very same command produces the
     original symbol again;
  3. cost: what one variant occupies with the overlay against what it occupies
     when the tree is copied, which is what the harness actually did.
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
FILES_IN_TREE = 400
HEADER_BYTES = 8 * 1024


def build_tree(root: Path):
    """A tree shaped like a real one: many headers, one translation unit."""
    inc = root / 'include'
    inc.mkdir(parents=True)
    for i in range(FILES_IN_TREE):
        (inc / f'h{i}.hpp').write_text(f'#pragma once\n// {"x" * HEADER_BYTES}\nint filler{i}();\n')
    src = root / 'main.cpp'
    src.write_text(
        '#include "include/h0.hpp"\n'
        'int original_symbol() { return 1; }\n'
    )
    return src


def compile_it(src: Path, out: Path, env):
    p = subprocess.run(['g++', '-c', str(src), '-o', str(out)],
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        print(p.stderr[-1500:])
    return p.returncode == 0


def symbols(obj: Path, env=None):
    # Under the overlay the object itself was written into the delta, so the
    # reader has to look through the overlay as well -- exactly as the next
    # stage of a real build does.
    p = subprocess.run(['nm', '-C', str(obj)], capture_output=True, text=True, env=env)
    return p.stdout


def du_bytes(path: Path):
    total = 0
    for p in path.rglob('*'):
        if p.is_file():
            total += p.stat().st_size
    return total


def main():
    work = Path(tempfile.mkdtemp(prefix='overlay-check-'))
    try:
        tree = work / 'tree'
        src = build_tree(tree)
        tree_bytes = du_bytes(tree)
        print(f'source tree: {FILES_IN_TREE + 1} files, {tree_bytes / 2**20:.1f} MiB')
        print()

        # The delta mirrors absolute paths, so the variant lives at
        # <delta><absolute path of the file it replaces>.
        delta = work / 'delta'
        variant = delta / str(src).lstrip('/')
        variant.parent.mkdir(parents=True)
        variant.write_text(
            '#include "include/h0.hpp"\n'
            'int variant_symbol() { return 2; }\n'
        )

        env_overlay = {**os.environ, 'LD_PRELOAD': LIB, 'CVISE_OVERLAY_DELTA': str(delta)}
        env_plain = {k: v for k, v in os.environ.items()
                     if k not in ('LD_PRELOAD', 'CVISE_OVERLAY_DELTA')}

        obj = work / 'through-overlay.o'
        ok = compile_it(src, obj, env_overlay)
        syms = symbols(obj, env_overlay) if ok else ''
        saw_variant = 'variant_symbol' in syms
        print(f'1. compiled the ORIGINAL path with the overlay: '
              f'{"the VARIANT was compiled" if saw_variant else "the original was compiled"}')
        print(f'   the file on disk still says: {src.read_text().splitlines()[1]!r}')

        obj2 = work / 'without-overlay.o'
        ok2 = compile_it(src, obj2, env_plain)
        syms2 = symbols(obj2, env_plain) if ok2 else ''
        saw_original = 'original_symbol' in syms2
        print(f'2. the same command without the overlay: '
              f'{"the original was compiled" if saw_original else "UNEXPECTED"}')

        variant_bytes = variant.stat().st_size
        print()
        print('3. what one variant costs:')
        print(f'   through the overlay      {variant_bytes:>12} bytes  (only the changed file)')
        print(f'   by copying the tree      {tree_bytes:>12} bytes  (what the harness did)')
        print(f'   ratio                    {tree_bytes / max(variant_bytes, 1):>12.0f}x')
        print(f'   at 52 parallel jobs      {52 * variant_bytes / 2**20:>12.1f} MiB'
              f'  vs {52 * tree_bytes / 2**20:.1f} MiB')

        print()
        good = saw_variant and saw_original
        print('OVERLAY WORKS FOR A REAL COMPILER' if good else 'OVERLAY DOES NOT WORK')
        return 0 if good else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
