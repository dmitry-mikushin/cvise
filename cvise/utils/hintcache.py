"""What clang_delta said about a file, kept so that a restart need not ask again.

clang_delta answers one question -- "what could be transformed in this file" --
and answering it costs seconds, because it parses the translation unit with all
its includes expanded. MEASURED on ns-projection: 2.3 s to 75 s per file, and a
full pass over the 1158 reducible files takes 3.4 h by medians.

That would be affordable once. It is not affordable repeatedly, and repeatedly
is what happens: an accepted reduction ends the batch, testing.py sends every
unfinished initialisation back to BEFORE_INIT, and the walk starts again from
the first file. MEASURED on a 5.9 h run: a reduction was accepted every 7m18s,
so 3.5% of an initialisation fitted between two of them, four transformations
burned 23.1 machine-hours at 100% CPU, and ClangHintsPass contributed to none
of the 173 accepted reductions of two runs. Not rarely -- never, and it could
not have been otherwise.

The answer, though, is almost always the same as last time. A publication
changes a handful of files out of 1158; the other thousand are byte-identical
and what clang_delta said about them still holds. So this keeps it, keyed by
everything that determines it, exactly as ccache does for the compiler -- which
on the same run answers 98% of 987 408 calls.

Deliberately NOT keyed by file path. Two files with identical content and
identical flags have identical hints, and a reduction produces such pairs
constantly.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import tempfile
from pathlib import Path

# Cheap, not cryptographic: this guards against a file changing, not against
# somebody constructing a collision. blake2b is faster than sha256 and the
# digest is truncated because 128 bits is far past the point where a collision
# would ever be seen.
DIGEST_BYTES = 16


def key_for(*parts: bytes) -> str:
    """One name for one question, from everything that decides the answer."""
    digest = hashlib.blake2b(digest_size=DIGEST_BYTES)
    for part in parts:
        # Length-prefixed, so that ('ab', 'c') and ('a', 'bc') are different
        # questions. Without this the transformation name and the standard
        # could run together and answer for each other.
        digest.update(len(part).to_bytes(4, 'big'))
        digest.update(part)
    return digest.hexdigest()


def fingerprint(path: Path) -> bytes:
    """What identifies a tool or a file that is too big to hash on every call.

    Size and mtime, as ccache and every build system does. It is wrong for a
    file rewritten within the same nanosecond at the same length, which is not
    a thing that happens to a compiled binary or a compilation database.
    """
    try:
        stat = path.stat()
    except OSError:
        return b'missing'
    return f'{stat.st_size}:{stat.st_mtime_ns}'.encode()


class HintCache:
    """A content-addressed store of clang_delta output.

    Lives under TMPDIR, which for a project reduction is the run's own state
    directory -- so it outlives the batch restarts it exists to survive, and a
    `--resume` on the same state continues to hit it.
    """

    #: Two levels of fan-out, so no directory holds more than a few hundred
    #: entries. A flat directory of 4632 files is fine on tmpfs and awful on
    #: anything else, and this file has no business knowing which it is on.
    FANOUT = 2

    def __init__(self, root: Path | None = None):
        if root is None:
            root = Path(tempfile.gettempdir()) / 'cvise-hint-cache'
        self.root = root
        self.hits = 0
        self.misses = 0
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logging.warning('hints will not be cached in %s: %s', root, e)
            self.root = None

    def _path(self, key: str) -> Path:
        return self.root / key[: self.FANOUT] / key[self.FANOUT :]

    def get(self, key: str) -> bytes | None:
        if self.root is None:
            return None
        try:
            data = self._path(key).read_bytes()
        except OSError:
            self.misses += 1
            return None
        self.hits += 1
        return data

    def put(self, key: str, value: bytes) -> None:
        """Store, or quietly do not.

        Through a temporary file and os.replace, because dozens of workers run
        at once and two of them asking the same question at the same time is
        ordinary. A reader must see either the whole answer or none of it; a
        half-written one would be parsed as a truncated hint bundle, which is
        the kind of corruption that looks like a very good reduction.
        """
        if self.root is None:
            return
        target = self._path(key)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=target.parent, prefix='.tmp')
            try:
                with os.fdopen(handle, 'wb') as f:
                    f.write(value)
                os.replace(temporary, target)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
                raise
        except OSError as e:
            # A full disk must cost the reduction nothing but the speed-up.
            logging.debug('could not cache hints: %s', e)
