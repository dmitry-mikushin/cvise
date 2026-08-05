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


# How to call each way of asking "is this file there", and which of them the
# library claims to handle -- read off the library, never listed here.
#
# A list written here is a second list. The overlay wrapped `stat` and not
# `__xstat64`; this file asked about `stat` and not `__xstat64`; the ninja in
# one project's build image calls `__xstat64`. Two hand-kept lists agreeing is a
# coincidence that has to be renewed at every edit, and when they stopped
# agreeing nothing said so:
#
#     stat()      -> ENOENT        <- python, clang, every probe written by hand
#     __xstat()   -> PRESENT       <- ninja
#     __xstat64() -> PRESENT
#
# so the build system saw an untouched tree, rebuilt nothing, and two runs ended
# with every candidate graded on the previous candidate's binary -- while this
# check reported the overlay healthy, because all it asked was whether the
# library had loaded.
#
# What is written here is only how each one is CALLED, which is a property of
# the C library's signatures and not of what this build wraps. Whether it is
# asked at all comes from the library's own exported symbols, so a wrapper that
# gets compiled out takes its entry in this check with it, and a wrapper that
# gets added is exercised without anyone remembering to add it here too.
AT_FDCWD = -100
HOW_TO_ASK = {
    'stat': 'fn(path, buf)',
    'stat64': 'fn(path, buf)',
    'lstat': 'fn(path, buf)',
    'lstat64': 'fn(path, buf)',
    'statx': f'fn({AT_FDCWD}, path, 0, 0, buf)',
    'fstatat': f'fn({AT_FDCWD}, path, buf, 0)',
    'fstatat64': f'fn({AT_FDCWD}, path, buf, 0)',
    '__xstat': 'fn(1, path, buf)',
    '__xstat64': 'fn(1, path, buf)',
    '__lxstat': 'fn(1, path, buf)',
    '__lxstat64': 'fn(1, path, buf)',
    '__fxstatat': f'fn(1, {AT_FDCWD}, path, buf, 0)',
    '__fxstatat64': f'fn(1, {AT_FDCWD}, path, buf, 0)',
}

# The least this may ever come down to. Reading the entry points off the library
# makes the check follow the library, including downwards: a build that wrapped
# nothing would be checked for nothing and would pass. These are the spellings
# whose absence has already cost two runs, so their absence is a failure and not
# a shorter list.
REQUIRED = ('stat', '__xstat64')


def stat_entry_points(library: str) -> dict:
    """Which ways of asking this library actually wraps, and how to call them."""
    try:
        listing = subprocess.run(
            ['nm', '-D', '--defined-only', library], capture_output=True, text=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        listing = ''
    wrapped = {line.split()[-1] for line in listing.splitlines() if ' T ' in line}
    if not wrapped:
        # nm is missing or said nothing. Falling back to the whole table would
        # test entry points the library may not wrap and fail for the wrong
        # reason; testing none would pass for the wrong reason. Ask about the
        # ones that must be there either way.
        return {name: HOW_TO_ASK[name] for name in REQUIRED}
    return {name: how for name, how in HOW_TO_ASK.items() if name in wrapped}

# Asked in a child, because the library is preloaded into the jobs and their
# compilers, never into the reducer. Prints the challenge answer, then one line
# per entry point that this C library actually has.
PROOF = r'''
import ctypes, sys

libc = ctypes.CDLL(None)
fn = libc[{symbol!r}]
fn.restype = ctypes.c_uint64
fn.argtypes = [ctypes.c_uint64]
print('challenge', fn({challenge}))

path = {probe!r}.encode()
buf = ctypes.create_string_buffer(1024)   # any struct stat variant fits
for name in {names!r}:
    try:
        fn = getattr(libc, name)
    except AttributeError:
        continue                          # this libc does not have it at all
    print(name, 'visible' if eval({calls!r}[name]) == 0 else 'hidden')
'''


def prove_overlay() -> int:
    """Prove the overlay works in a process built exactly like a job's.

    Two independent questions, because either one passing alone is compatible
    with a reduction that grades every candidate against untouched code.

    Did this code run? The answer is a function of a random challenge, so a stub
    returning a constant cannot forge it and a missing symbol cannot be mistaken
    for a negative answer.

    Is the redirection live, on every entry point? A path that exists only in the
    delta must be visible, and it must be visible whichever way it is asked. This
    was documented here and not actually performed: the probe file was written
    into the delta and nothing ever looked for it, so the run that ended with the
    build reading straight through the overlay had been told the overlay was
    proven.
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
        if Path(PROBE_PATH).exists():
            # The probe proves redirection by being visible where only the delta
            # has it. If the root has it too, every answer would be "visible"
            # whether anything was redirected or not.
            raise OverlayNotProvenError(
                f'{PROBE_PATH} exists outside the delta, so it cannot show redirection'
            )
        env = {**os.environ, 'LD_PRELOAD': lib, DELTA_ENV: delta, ROOT_ENV: '/'}
        asked = stat_entry_points(lib)
        missing = [name for name in REQUIRED if name not in asked]
        if missing:
            raise OverlayNotProvenError(
                f'{lib} does not wrap {", ".join(missing)}. A build tool that spells '
                'the question that way reads the original tree, finds nothing changed '
                'and rebuilds nothing, and its test then runs the previous '
                "candidate's binary"
            )
        code = PROOF.format(
            symbol=SYMBOL,
            challenge=challenge,
            probe=PROBE_PATH,
            names=sorted(asked),
            calls=asked,
        )
        proc = subprocess.run([sys.executable, '-c', code], capture_output=True,
                              text=True, env=env)

    if proc.returncode != 0:
        raise OverlayNotProvenError(
            f'a child with {lib} preloaded could not answer: {proc.stderr.strip()[:200]}'
        )

    answers = {}
    challenge_answer = None
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(' ')
        if key == 'challenge':
            challenge_answer = value
        else:
            answers[key] = value

    if challenge_answer is None:
        raise OverlayNotProvenError(f'no answer at all: {proc.stdout.strip()[:80]}')
    try:
        answer = int(challenge_answer)
    except ValueError:
        raise OverlayNotProvenError(f'the answer was not a number: {challenge_answer[:80]}')
    if answer != expected:
        raise OverlayNotProvenError(
            f'the answer to the challenge is wrong ({answer} instead of {expected}), so the '
            'library either does not redirect or is not the overlay this build expects'
        )

    if not answers:
        raise OverlayNotProvenError(
            'the library loaded but no way of asking whether a file exists could be '
            'tested, so nothing shows the redirection is live'
        )
    blind = sorted(name for name, seen in answers.items() if seen != 'visible')
    if blind:
        raise OverlayNotProvenError(
            f'{len(answers) - len(blind)} of {len(answers)} ways of asking whether a file '
            f'exists are redirected, and these are not: {", ".join(blind)}. A build tool '
            'that calls one of those reads the original tree, finds nothing changed and '
            'rebuilds nothing, and its test then runs the previous candidate\'s binary'
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

    The filesystem root is refused for the opposite reason. It is a legal value
    and it means "redirect everything" -- and everything includes the job's own
    sockets, /proc, /dev and the scratch of whatever its tools are written in.
    No reduction wants that, so a run arriving here with it has computed a root
    wrongly, and the useful thing to do is say so rather than to obey.
    """
    values = [str(Path(r)) for r in roots]
    bad = [v for v in values if ':' in v]
    if bad:
        raise CViseError(
            'these directories cannot be isolated because their names contain a colon, '
            f'which separates one from the next: {", ".join(bad)}'
        )
    if '/' in values:
        raise CViseError(
            'the filesystem root was given as a tree to isolate, which would redirect every '
            'path a job touches, including the ones with nothing to do with the reduction'
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
