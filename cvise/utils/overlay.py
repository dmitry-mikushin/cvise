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
import subprocess
import sys
import tempfile
from pathlib import Path
import secrets

from cvise.utils.error import CViseError

# Must match overlay/src/libfakechroot.h, which is built alongside this.
OVERLAY_MAGIC = 0x6376697365D1AC70
PROBE_PATH = '/.cvise-overlay-probe'
DELTA_ENV = 'CVISE_OVERLAY_DELTA'
SYMBOL = 'cvise_overlay_selfcheck'
LIB_ENV = 'CVISE_OVERLAY_LIB'
ROOT_ENV = 'CVISE_OVERLAY_ROOT'


class OverlayMissingError(CViseError):
    def __str__(self):
        return (
            'The reduction overlay is not installed. Refusing to start: without it every '
            'candidate is prepared by copying the project, which restamps every file, so the '
            'build system rebuilds everything for every candidate and a reduction of any real '
            'project does not finish. This is built as part of C-Vise, so its absence means an incomplete '
            f'installation: it belongs at {INSTALLED_LIB}.'
        )


class OverlayNotProvenError(CViseError):
    def __init__(self, reason):
        self.reason = reason

    def __str__(self):
        return (
            f'The reduction overlay could not prove itself: {self.reason}. '
            'Refusing to continue: without the overlay the compiler reads the original '
            'sources, so every candidate would be graded against code the reduction never '
            f'changed. Check that the overlay library is in LD_PRELOAD and that {DELTA_ENV} points '
            'at the delta directory of this run.'
        )


def overlay_configured() -> bool:
    """Is this run supposed to go through the overlay at all?

    Naming the library IS the request to use it. The delta cannot be the
    condition: there is one per job now, created by C-Vise itself, so keying
    off it meant the check never fired in the only mode that uses the overlay
    -- the guard was reading a variable that the user no longer sets.
    """
    return bool(library_path())


def prove_overlay() -> int:
    """Prove the overlay works in a process built exactly like a job's.

    Asking the question inside C-Vise itself would answer about the wrong
    process: the library is preloaded into the interestingness test and its
    compilers, not into the reducer. So the proof is run in a child set up the
    same way a job will be -- same library, same kind of delta -- and what it
    demonstrates is what the jobs will get.
    """
    lib = library_path()
    if not lib:
        raise OverlayNotProvenError(f'{LIB_ENV} is not set')
    if not Path(lib).exists():
        raise OverlayNotProvenError(f'{LIB_ENV}={lib} does not exist')

    challenge = secrets.randbits(64)
    expected = ((challenge ^ OVERLAY_MAGIC) + 1) & 0xFFFFFFFFFFFFFFFF

    with tempfile.TemporaryDirectory(prefix='cvise-overlay-proof-') as delta:
        probe = Path(delta) / PROBE_PATH.lstrip('/')
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text('probe\n')
        env = {**os.environ, 'LD_PRELOAD': lib, DELTA_ENV: delta, ROOT_ENV: '/'}
        code = (
            'import ctypes\n'
            'fn = ctypes.CDLL(None)[%r]\n'
            'fn.restype = ctypes.c_uint64\n'
            'fn.argtypes = [ctypes.c_uint64]\n'
            'print(fn(%d))\n' % (SYMBOL, challenge)
        )
        proc = subprocess.run([sys.executable, '-c', code], capture_output=True,
                              text=True, env=env)

    if proc.returncode != 0:
        raise OverlayNotProvenError(
            f'a child with {lib} preloaded could not answer: {proc.stderr.strip()[:200]}'
        )
    try:
        answer = int(proc.stdout.strip())
    except ValueError:
        raise OverlayNotProvenError(f'the answer was not a number: {proc.stdout.strip()[:80]}')
    if answer != expected:
        raise OverlayNotProvenError(
            f'the answer to the challenge is wrong ({answer} instead of {expected}), so the '
            'library either does not redirect or is not the overlay this build expects'
        )
    return answer


# Where the library is installed alongside C-Vise's other helpers, and where it
# lands in a build tree, both filled in at configure time exactly as the other
# helper paths are.
INSTALLED_LIB = os.path.join('@CMAKE_INSTALL_FULL_LIBEXECDIR@', '@cvise_PACKAGE@', 'libcvise_overlay.so')
BUILT_LIB = '@cvise_OVERLAY_BUILD_LIB@'


def library_path() -> str:
    """The overlay library, found the way every other helper is found.

    This is not a knob. Whether the reduction goes through an overlay is a
    property of how C-Vise is built and installed, not a decision to leave on
    the command line: a user who forgets it gets a reduction that copies the
    whole tree for every candidate, and one who mistypes it gets no overlay at
    all with no indication that anything is different. The environment variable
    stays only so that a developer can point at a build tree.

    The build tree comes before the installed copy. This copy of C-Vise and this
    copy of the library were made together and are the only pair known to agree;
    reaching for an installed one means a change is tested against whatever was
    installed last, and since a stale overlay does not fail but merely answers
    differently, that is a whole afternoon of believing a fixed bug is not
    fixed. It was.
    """
    override = os.environ.get(LIB_ENV, '')
    if override:
        return override
    for candidate in (BUILT_LIB, INSTALLED_LIB):
        if '@' not in candidate and os.path.exists(candidate):
            return candidate
    return ''


WHITEOUT_SUFFIX = '.cvise-whiteout'


def prepare_job_delta(folder, variants, expected=()) -> tuple[Path, list[Path]]:
    """Put this job's variants where the overlay will serve them from.

    The reducer produces a candidate as a file in the job's own scratch
    directory, but the build under test opens the project by its real path and
    knows nothing about scratch directories. The overlay bridges that: a file
    placed at <delta>/<original absolute path> is what every process in this job
    sees when it opens the original. Nothing is copied except the files the
    candidate actually changed.

    Those files are also the answer to "what did this candidate change", which
    is what makes a cheap rejection possible: the compiler can be asked about
    them alone, with their own flags, before anything as expensive as a build is
    started. So they are returned rather than discarded.
    """
    delta = Path(folder) / '.cvise-delta'
    present: set[Path] = set()
    changed: list[Path] = []
    for original, produced in variants:
        if produced.is_dir():
            for path in sorted(produced.rglob('*')):
                if path.is_file():
                    target = original / path.relative_to(produced)
                    present.add(target)
                    if _place(delta, target, path):
                        changed.append(target)
        else:
            present.add(original)
            if _place(delta, original, produced):
                changed.append(original)

    # A file the candidate deleted has to be recorded as deleted. Producing no
    # entry for it means the overlay falls through to the shared tree and the
    # build reads the original -- so deleting a file would look interesting
    # every single time, and the reduction would happily delete the entire
    # project and call it a success. It does exactly that without this.
    for original in expected:
        original = Path(original)
        if original in present:
            continue
        marker = delta / (str(original).lstrip('/') + WHITEOUT_SUFFIX)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    return delta, changed


def _place(delta: Path, original: Path, produced: Path) -> bool:
    """Put a file in the delta only if it actually differs; say whether it did.

    The delta is what the build sees instead of the project, and a file placed
    there is a file the build must rebuild -- it has a new timestamp and, as far
    as the build system can tell, new content. Placing every file of the project
    would therefore cost a full rebuild for every candidate, which for a project
    of any size is the whole reduction. Only what this candidate changed belongs
    here; everything else is read from the shared tree, unchanged and untouched,
    which is the entire point of an overlay.
    """
    try:
        if (
            original.is_file()
            and original.read_bytes() == produced.read_bytes()
            # Mode counts as much as content: a candidate that only makes a file
            # non-executable changes what the build does, and dropping it from
            # the delta because the bytes match means the job is graded on a
            # file it was never given.
            and (original.stat().st_mode & 0o7777) == (produced.stat().st_mode & 0o7777)
        ):
            return False
    except OSError:
        pass
    target = delta / str(original).lstrip('/')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(produced, target)
    shutil.copymode(produced, target)
    return True


def roots_value(roots) -> str:
    """The trees this job may write to, as the overlay expects them.

    Colon-separated, the way a search path is -- so a directory whose name
    contains a colon cannot be expressed, and the consequence of pretending
    otherwise is not a syntax error but a tree that is silently left shared.
    That is the most expensive failure this program has: the reduction keeps
    running and answers about a build nobody described.
    """
    values = [str(Path(r)) for r in roots]
    bad = [v for v in values if ':' in v]
    if bad:
        raise CViseError(
            'these directories cannot be isolated because their names contain a colon, '
            f'which separates one from the next: {", ".join(bad)}'
        )
    return ':'.join(values)


def job_environment(env: dict, delta: Path, roots) -> dict:
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
    env[ROOT_ENV] = roots_value(roots)
    return env
