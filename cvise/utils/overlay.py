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
import secrets

from cvise.utils.error import CViseError

# Must match src/libfakechroot.h in the overlay.
OVERLAY_MAGIC = 0x6376697365D1AC70
PROBE_PATH = '/.cvise-overlay-probe'
DELTA_ENV = 'CVISE_OVERLAY_DELTA'
SYMBOL = 'cvise_overlay_selfcheck'


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
