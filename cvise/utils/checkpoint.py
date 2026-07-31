"""Checkpoint/resume support for long-running reductions.

A reduction can be interrupted (e.g. killed) at any moment and resumed later from the last completed pass instead of
restarting from scratch. The on-disk format (version 1) is shared with C-Reduce so that either tool can produce and
consume it; it is documented in ``checkpoint-format.md``.

The reduced test case itself is not stored: both tools rewrite the test case in place as soon as a smaller interesting
variant is found, so the file on disk already is the best result so far. The checkpoint only records where in the pass
schedule the reduction stood, plus fingerprints (sha256) used to refuse a resume that would be meaningless.

Granularity is a pass boundary: an interrupted pass is replayed from its start. A category that runs its passes
interleaved is a single unit, so the boundary there is the category.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cvise.utils.error import CViseError

if TYPE_CHECKING:
    from cvise.utils.statistics import PassStatistic
    from cvise.utils.testing import TestManager

CHECKPOINT_VERSION = 1


class CheckpointError(CViseError):
    """Raised when a checkpoint cannot be resumed; produces a clean, non-traceback error exit."""


@dataclass
class Position:
    """Names the pass to run NEXT: everything strictly before this position is done."""

    phase: str  # the pass category name
    round: int  # how many times the looped category has been (re)started; 0-based
    index: int  # zero-based position within the category's pass list
    pass_: str  # identity of that pass, "<name>::<arg>", to detect schedule changes


@dataclass
class FileFingerprint:
    name: str
    size: int
    sha256: str


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    if path.is_dir():
        # Hash directory contents deterministically (sorted by relative path).
        for p in sorted(q for q in path.rglob('*') if q.is_file() and not q.is_symlink()):
            h.update(str(p.relative_to(path)).encode('utf-8'))
            h.update(b'\0')
            with open(p, 'rb') as f:
                for chunk in iter(lambda: f.read(2**18), b''):
                    h.update(chunk)
    else:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(2**18), b''):
                h.update(chunk)
    return h.hexdigest()


def _fingerprint(path: Path) -> FileFingerprint:
    size = sum(p.stat().st_size for p in ([path] if path.is_file() else path.rglob('*')) if p.is_file())
    return FileFingerprint(name=str(path), size=size, sha256=_sha256_of(path))


def _original_of(path: Path) -> dict:
    """Fingerprint of the pristine input, which C-Vise keeps next to the test case as <name>.orig.

    The test case itself cannot identify a reduction on resume: it is overwritten with every smaller
    interesting variant, so a run killed in the middle of a pass legitimately leaves a file that differs
    from the one the last checkpoint recorded. The untouched input does identify it.
    """
    backup = Path(f'{path}.orig')
    if not backup.exists():
        return {}
    return {'original_sha256': _sha256_of(backup)}


def _statistics_to_dict(pass_statistic: PassStatistic) -> dict:
    result = {}
    for name, stat in pass_statistic._stats.items():
        result[name] = {
            'worked': stat.worked,
            'failed': stat.failed,
            'totally_executed': stat.totally_executed,
            'total_seconds': stat.total_seconds,
            'total_size_delta': stat.total_size_delta,
        }
    return result


class Checkpoint:
    """Reads, writes and validates a reduction checkpoint file."""

    def __init__(self, path: Path, tool_version: str) -> None:
        self.path = path
        self.tool_version = tool_version
        # Populated when a resumable checkpoint was loaded; consumed once by the driver.
        self.resume_position: Position | None = None
        self._resume_total_file_size: int | None = None
        self._resume_orig_total_file_size: int | None = None
        self._resume_statistics: dict | None = None

    # -- writing --------------------------------------------------------------

    def save(self, test_manager: TestManager, position: Position) -> None:
        """Write the checkpoint atomically after a pass (or interleaved category) completed."""
        data = {
            'checkpoint_version': CHECKPOINT_VERSION,
            'tool': 'cvise',
            'tool_version': self.tool_version,
            'created': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'workdir': str(Path.cwd()),
            'interestingness_test': {
                'path': str(test_manager.test_script),
                'sha256': _sha256_of(test_manager.test_script),
            },
            'test_cases': [
                {'name': fp.name, 'size': fp.size, 'sha256': fp.sha256, **_original_of(Path(fp.name))}
                for fp in (_fingerprint(tc) for tc in sorted(test_manager.test_cases))
            ],
            'position': {
                'phase': position.phase,
                'round': position.round,
                'index': position.index,
                'pass': position.pass_,
            },
            'total_file_size': test_manager.total_file_size,
            'orig_total_file_size': test_manager.orig_total_file_size,
            'statistics': _statistics_to_dict(test_manager.pass_statistic),
        }
        tmp_path = self.path.with_name(self.path.name + '.tmp')
        with open(tmp_path, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.path)
        logging.debug(
            'Wrote checkpoint at phase=%s round=%d index=%d pass=%s',
            position.phase,
            position.round,
            position.index,
            position.pass_,
        )

    def remove(self) -> None:
        """Remove the checkpoint file when the reduction finished normally."""
        try:
            self.path.unlink()
            logging.debug('Removed checkpoint %s', self.path)
        except FileNotFoundError:
            pass

    # -- reading / validating -------------------------------------------------

    def load(self, test_manager: TestManager, pass_group: dict) -> bool:
        """Load and validate the checkpoint for resuming.

        Returns True if a valid checkpoint was loaded and the reduction should resume; False if the checkpoint does not
        exist (start a normal reduction). Raises CheckpointError, with a message naming the exact mismatch and telling
        the user what to do, when the checkpoint exists but cannot be resumed.
        """
        if not self.path.exists():
            return False

        try:
            with open(self.path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise self._refuse(f'the checkpoint file {self.path} could not be read ({e})') from None

        version = data.get('checkpoint_version')
        if version != CHECKPOINT_VERSION:
            raise self._refuse(
                f'checkpoint_version {version!r} is not understood (this build supports version {CHECKPOINT_VERSION})'
            )

        self._validate_interestingness_test(data, test_manager)
        self._validate_test_cases(data, test_manager)
        position = self._validate_position(data, pass_group)

        self.resume_position = position
        self._resume_total_file_size = data.get('total_file_size')
        self._resume_orig_total_file_size = data.get('orig_total_file_size')
        self._resume_statistics = data.get('statistics') or {}
        logging.info(
            'Resuming from checkpoint %s: phase=%s round=%d index=%d pass=%s',
            self.path,
            position.phase,
            position.round,
            position.index,
            position.pass_,
        )
        return True

    def _validate_interestingness_test(self, data: dict, test_manager: TestManager) -> None:
        recorded = (data.get('interestingness_test') or {}).get('sha256')
        actual = _sha256_of(test_manager.test_script)
        if recorded != actual:
            raise self._refuse(
                f'the interestingness test {test_manager.test_script} has changed: '
                f'checkpoint recorded sha256 {_short(recorded)} but it is now {_short(actual)}'
            )

    def _validate_test_cases(self, data: dict, test_manager: TestManager) -> None:
        recorded_cases = {c['name']: c for c in data.get('test_cases', [])}
        current_names = {str(tc) for tc in test_manager.test_cases}

        for name, recorded in recorded_cases.items():
            path = Path(name)
            if name not in current_names or not path.exists():
                raise self._refuse(f'the test case {name} named in the checkpoint is missing')

            # The pristine input is what identifies the reduction; the test case itself keeps changing
            # while a pass runs, so it is only required not to have grown.
            backup = Path(f'{name}.orig')
            recorded_original = recorded.get('original_sha256')
            if recorded_original and backup.exists():
                actual_original = _sha256_of(backup)
                if recorded_original != actual_original:
                    raise self._refuse(
                        f'{backup} is not the input this checkpoint was made from: '
                        f'recorded sha256 {_short(recorded_original)} but it is now {_short(actual_original)}'
                    )

            recorded_size = recorded.get('size')
            actual_size = _fingerprint(path).size
            if recorded_size is not None and actual_size > recorded_size:
                raise self._refuse(
                    f'the test case {name} grew from {recorded_size} to {actual_size} bytes since the '
                    'checkpoint was written, so it is not what that reduction left behind'
                )

    def _validate_position(self, data: dict, pass_group: dict) -> Position:
        pos = data.get('position') or {}
        try:
            phase = pos['phase']
            index = int(pos['index'])
            round_ = int(pos.get('round', 0))
            pass_name = pos['pass']
        except (KeyError, TypeError, ValueError) as e:
            raise self._refuse(f'the checkpoint position is malformed ({e})') from None

        if phase not in pass_group:
            raise self._refuse(
                f'the checkpoint phase {phase!r} is not present in the current schedule '
                '(the pass group changed between runs)'
            )

        passes = pass_group[phase]
        if index >= len(passes):
            raise self._refuse(
                f'the checkpoint position index {index} is out of range for phase {phase!r} '
                f'(it has {len(passes)} passes; the schedule changed between runs)'
            )
        actual_pass = str(passes[index])
        if actual_pass != pass_name:
            raise self._refuse(
                f'the pass at position index {index} in phase {phase!r} is {actual_pass!r}, '
                f'but the checkpoint expects {pass_name!r} (the schedule changed between runs)'
            )
        return Position(phase=phase, round=round_, index=index, pass_=pass_name)

    def restore_statistics(self, pass_statistic: PassStatistic) -> None:
        """Restore the statistics so the final report covers the whole reduction, not just the resumed part."""
        if not self._resume_statistics:
            return
        from cvise.utils.statistics import SinglePassStatistic

        for name, values in self._resume_statistics.items():
            stat = pass_statistic._stats.get(name)
            if stat is None:
                stat = SinglePassStatistic(name)
                pass_statistic._stats[name] = stat
            stat.worked += values.get('worked', 0)
            stat.failed += values.get('failed', 0)
            stat.totally_executed += values.get('totally_executed', 0)
            stat.total_seconds += values.get('total_seconds', 0.0)
            stat.total_size_delta += values.get('total_size_delta', 0)

    @property
    def resume_total_file_size(self) -> int | None:
        return self._resume_total_file_size

    @property
    def resume_orig_total_file_size(self) -> int | None:
        return self._resume_orig_total_file_size

    def _refuse(self, reason: str) -> CheckpointError:
        return CheckpointError(
            f'Cannot resume from checkpoint: {reason}.\n'
            f'To start a fresh reduction, delete the checkpoint file {self.path} or rerun without --checkpoint.'
        )


def _short(sha: str | None) -> str:
    if not sha:
        return '(none)'
    return sha[:12]
