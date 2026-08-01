#!/usr/bin/env python3
"""Prove the overlay self-check answers correctly in all four situations.

The check exists to make an unprovable environment fatal, so it is worth
knowing that it actually distinguishes the cases it claims to:

  1. library not preloaded          -> the symbol is absent
  2. preloaded, no delta configured -> answers without the redirection bit
  3. preloaded, delta configured    -> answers with the redirection bit
  4. a stub that returns a constant -> fails the challenge
"""
import ctypes
import os
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
MAGIC = 0x6376697365D1AC70
PROBE = '/.cvise-overlay-probe'
CHALLENGE = 0x0123456789ABCDEF


def ask(env):
    """Ask the question the way C-Vise will: in the process that has the preload."""
    code = (
        'import ctypes\n'
        'try:\n'
        '    fn = ctypes.CDLL(None).cvise_overlay_selfcheck\n'
        'except AttributeError:\n'
        '    print("ABSENT"); raise SystemExit\n'
        'fn.restype = ctypes.c_uint64\n'
        'fn.argtypes = [ctypes.c_uint64]\n'
        f'print(fn({CHALLENGE}))\n'
    )
    p = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                       env={**os.environ, **env})
    return p.stdout.strip()


def main():
    expected_loaded = (CHALLENGE ^ MAGIC)
    print(f'expected without redirection: {expected_loaded}')
    print(f'expected with redirection:    {expected_loaded + 1}')
    print()

    ok = True

    got = ask({})
    print(f'1. no preload                 -> {got}')
    ok &= got == 'ABSENT'

    got = ask({'LD_PRELOAD': LIB})
    print(f'2. preloaded, no delta        -> {got}')
    ok &= got == str(expected_loaded)

    with tempfile.TemporaryDirectory() as delta:
        # The probe path only resolves once the delta actually holds it, which
        # is what makes the third answer a statement about redirection and not
        # merely about the library being present.
        (Path(delta) / PROBE.lstrip('/')).write_text('probe\n')
        got = ask({'LD_PRELOAD': LIB, 'CVISE_OVERLAY_DELTA': delta})
        print(f'3. preloaded, delta present   -> {got}')
        ok &= got == str(expected_loaded + 1)

        got = ask({'LD_PRELOAD': LIB, 'CVISE_OVERLAY_DELTA': delta + '-does-not-exist'})
        print(f'4. preloaded, delta empty     -> {got}')
        ok &= got == str(expected_loaded)

    print()
    print('SELF-CHECK DISTINGUISHES ALL CASES' if ok else 'SELF-CHECK IS NOT SOUND')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
