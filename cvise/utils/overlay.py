"""Refuse to reduce through an overlay that cannot prove it is there.

A reduction driven through the LD_PRELOAD overlay is only meaningful if the
overlay is actually loaded and actually redirecting. Nothing about that is
visible from the outside: a dropped LD_PRELOAD, a static binary, a wrapper that
scrubs the environment, or an unset delta all look exactly like a healthy setup
-- right up to the point where the compiler reads the ORIGINAL sources, every
candidate compiles, every verdict says "interesting", and the run produces a
confident answer about nothing at all.

So the overlay is asked, and it has to answer two independent questions:

  1. did this code run?  The answer is a function of a random challenge, so a
     stub returning a constant cannot forge it and a missing symbol cannot be
     mistaken for a negative answer.
  2. is the redirection live?  The answer differs depending on whether a probe
     path really gets rewritten into the delta.

Either question unanswered is fatal.
"""

import ctypes
import os
import shutil
from pathlib import Path
import secrets

from cvise.utils.error import CViseError

# Must match src/libfakechroot.h in the overlay.
OVERLAY_MAGIC = 0x6376697365D1AC70
PROBE_PATH = '/.cvise-overlay-probe'
DELTA_ENV = 'CVISE_OVERLAY_DELTA'
SYMBOL = 'cvise_overlay_selfcheck'
LIB_ENV = 'CVISE_OVERLAY_LIB'
ROOT_ENV = 'CVISE_OVERLAY_ROOT'


class OverlayNotProvenError(CViseError):
    def __init__(self, reason):
        self.reason = reason

    def __str__(self):
        return (
            f'The reduction overlay could not prove itself: {self.reason}. '
            'Refusing to continue: without the overlay the compiler reads the original '
            'sources, so every candidate would be graded against code the reduction never '
            f'changed. Check that libfakechroot is in LD_PRELOAD and that {DELTA_ENV} points '
            'at the delta directory of this run.'
        )


def overlay_configured() -> bool:
    """Is this run supposed to go through the overlay at all?

    Asking for a delta IS the request to use the overlay, so that is the
    condition. There is no separate switch to forget: a run that names a delta
    and does not get redirection is a run whose every verdict is meaningless,
    and it must not start.
    """
    return bool(os.environ.get(DELTA_ENV, ''))


def prove_overlay() -> int:
    """Run both halves of the self-check. Returns the answer for logging."""
    delta = os.environ.get(DELTA_ENV, '')
    if not delta:
        raise OverlayNotProvenError(f'{DELTA_ENV} is not set')

    try:
        fn = ctypes.CDLL(None)[SYMBOL]
    except AttributeError:
        raise OverlayNotProvenError(
            f'the symbol {SYMBOL} is not in this process, so the overlay library is not loaded'
        )
    fn.restype = ctypes.c_uint64
    fn.argtypes = [ctypes.c_uint64]

    challenge = secrets.randbits(64)
    answer = fn(challenge)
    expected_loaded = (challenge ^ OVERLAY_MAGIC) & 0xFFFFFFFFFFFFFFFF

    if answer == expected_loaded:
        raise OverlayNotProvenError(
            f'the library answers, but it does not redirect {PROBE_PATH} -- '
            f'{DELTA_ENV}={delta} holds no probe, so the overlay is loaded but inert'
        )
    if answer != (expected_loaded + 1) & 0xFFFFFFFFFFFFFFFF:
        raise OverlayNotProvenError(
            f'the answer to the challenge is wrong ({answer}), so whatever exports '
            f'{SYMBOL} is not the overlay this build expects'
        )
    return answer


def library_path() -> str:
    """The overlay library to preload into the interestingness test, if any."""
    return os.environ.get(LIB_ENV, '')


def prepare_job_delta(folder, variants) -> Path:
    """Put this job's variants where the overlay will serve them from.

    The reducer produces a candidate as a file in the job's own scratch
    directory, but the build under test opens the project by its real path and
    knows nothing about scratch directories. The overlay bridges that: a file
    placed at <delta>/<original absolute path> is what every process in this job
    sees when it opens the original. Nothing is copied except the files the
    candidate actually changed.
    """
    delta = Path(folder) / '.cvise-delta'
    for original, produced in variants:
        target = delta / str(original).lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        if produced.is_dir():
            shutil.copytree(produced, target, dirs_exist_ok=True)
        else:
            shutil.copyfile(produced, target)
    return delta


def job_environment(env: dict, delta: Path, root: Path) -> dict:
    """Point one job's processes at its own delta.

    The proof that the overlay is loaded has to happen where the compiling
    happens. Checking it once in the parent says nothing about the children,
    which is where the redirection actually has to work.
    """
    lib = library_path()
    if not lib:
        return env
    env = dict(env)
    preload = env.get('LD_PRELOAD', '')
    env['LD_PRELOAD'] = f'{lib}:{preload}' if preload else lib
    env[DELTA_ENV] = str(delta)
    env[ROOT_ENV] = str(root)
    return env
