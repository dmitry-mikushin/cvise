import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from cvise.passes.abstract import AbstractPass, BinaryState, PassResult
from cvise.passes.clang import sources_of


@dataclass
class DirBinaryState:
    """Binary search inside one translation unit of a directory test case.

    The search itself only makes sense within a single unit, so the position in
    the tree is carried alongside it and moves on when a unit is exhausted.
    """

    file_index: int
    inner: BinaryState

    def __repr__(self):
        return f'DirBinaryState(file #{self.file_index}, {self.inner})'


class ClangBinarySearchPass(AbstractPass):
    def __init__(
        self,
        arg: str,
        external_programs: dict[str, str | None],
        user_clang_delta_std: str | None = None,
        clang_delta_preserve_routine: str | None = None,
        compilation_database: str | None = None,
        **kwargs,
    ):
        self._compilation_database = compilation_database
        super().__init__(
            arg=arg,
            external_programs=external_programs,
            user_clang_delta_std=user_clang_delta_std,
            clang_delta_preserve_routine=clang_delta_preserve_routine,
            **kwargs,
        )
        self._user_clang_delta_std = user_clang_delta_std
        self._clang_delta_preserve_routine = clang_delta_preserve_routine
        # Remembered from new(), so that moving on to the next unit does not
        # end up querying clang_delta without any time limit.
        self._job_timeout = None

    def check_prerequisites(self):
        return self.check_external_program('clang_delta')

    def detect_best_standard(self, test_case: Path, timeout):
        best = None
        best_count = -1
        for std in ('c++98', 'c++11', 'c++14', 'c++17', 'c++20', 'c++2b'):
            start = time.monotonic()
            instances = self.count_instances(test_case, std, timeout)
            took = time.monotonic() - start

            # prefer newer standard if the # of instances is equal
            if instances >= best_count:
                best = std
                best_count = instances
            logging.debug('available transformation opportunities for %s: %d, took: %.2f s' % (std, instances, took))
        logging.info('using C++ standard: %s with %d transformation opportunities' % (best, best_count))
        # Use the best standard option
        return best

    def supports_dir_test_cases(self) -> bool:
        return True

    def _standard_for(self, test_case: Path, job_timeout):
        if self._user_clang_delta_std:
            return self._user_clang_delta_std
        if self._compilation_database:
            # The build says which standard the file uses; nothing to detect.
            return None
        return self.detect_best_standard(test_case, job_timeout)

    def _state_from_file(self, sources, file_index: int, std, job_timeout, original_test_case, test_case: Path):
        """First unit at or after file_index that has anything to transform."""
        while file_index < len(sources):
            target = sources[file_index]
            key = target
            if original_test_case is not None:
                key = Path(original_test_case) / target.relative_to(test_case)
            inner = BinaryState.create(self.count_instances(target, std, job_timeout, key))
            if inner is not None:
                return DirBinaryState(file_index=file_index, inner=attach_clang_delta_std(inner, std))
            file_index += 1
        return None

    def new(self, test_case: Path, job_timeout, *args, **kwargs):
        self._job_timeout = job_timeout
        original_test_case = kwargs.get('original_test_case')
        if test_case.is_dir():
            sources = sources_of(test_case)
            if not sources:
                return None
            std = self._standard_for(sources[0], job_timeout)
            return self._state_from_file(sources, 0, std, job_timeout, original_test_case, test_case)

        std = self._standard_for(test_case, job_timeout)
        state = BinaryState.create(self.count_instances(test_case, std, job_timeout, original_test_case))
        return attach_clang_delta_std(state, std)

    def advance(self, test_case: Path, state):
        if isinstance(state, DirBinaryState):
            inner = state.inner.advance()
            if inner is not None:
                return DirBinaryState(state.file_index, attach_clang_delta_std(inner, state.inner.clang_delta_std))
            # This unit is done; the pass is not.
            return self._state_from_file(
                sources_of(test_case), state.file_index + 1, state.inner.clang_delta_std,
                self._job_timeout, None, test_case
            )
        new_state = state.advance()
        return attach_clang_delta_std(new_state, state.clang_delta_std)

    def advance_on_success(self, test_case: Path, state, succeeded_state, *args, **kwargs):
        if isinstance(state, DirBinaryState):
            succeeded_inner = succeeded_state.inner
            instances = succeeded_inner.real_num_instances - succeeded_inner.real_chunk()
            inner = state.inner.advance_on_success(instances)
            if inner is not None:
                inner.real_num_instances = None
                return DirBinaryState(state.file_index, attach_clang_delta_std(inner, state.inner.clang_delta_std))
            return self._state_from_file(
                sources_of(test_case), state.file_index + 1, state.inner.clang_delta_std,
                self._job_timeout, None, test_case
            )
        instances = succeeded_state.real_num_instances - succeeded_state.real_chunk()
        new_state = state.advance_on_success(instances)
        if new_state:
            new_state.real_num_instances = None
        return attach_clang_delta_std(new_state, state.clang_delta_std)

    def _compilation_database_args(self, lookup_path: Path) -> list[str]:
        """Options telling clang_delta to parse with the flags the build uses.

        The pass may work on a copy in a scratch directory, so the flags are
        looked up under the path the build system knows.
        """
        if not self._compilation_database:
            return []
        return [
            f'--compilation-database={self._compilation_database}',
            f'--compilation-database-key={Path(lookup_path).resolve()}',
        ]

    def count_instances(self, test_case: Path, std, timeout, lookup_key=None):
        args = [
            self.external_programs['clang_delta'],
            f'--query-instances={self.arg}',
        ]
        if not self._compilation_database:
            args.append(f'--std={std}')
        args += self._compilation_database_args(test_case if lookup_key is None else lookup_key)
        if self._clang_delta_preserve_routine:
            args.append(f'--preserve-routine="{self._clang_delta_preserve_routine}"')
        cmd = args + [str(test_case)]

        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            logging.warning(f'clang_delta --query-instances (--std={std}) {timeout}s timeout reached')
            return 0
        except subprocess.SubprocessError as e:
            logging.warning(f'clang_delta --query-instances (--std={std}) failed: {e}')
            return 0

        if proc.returncode != 0:
            logging.warning(
                f'clang_delta --query-instances failed with exit code {proc.returncode}: {proc.stderr.strip()}'
            )

        m = re.match('Available transformation instances: ([0-9]+)$', proc.stdout)

        if m is None:
            return 0
        else:
            return int(m.group(1))

    def parse_stderr(self, state, stderr):
        for line in stderr.split(b'\n'):
            if line.startswith(b'Available transformation instances:'):
                real_num_instances = int(line.decode().split(':')[1])
                state.real_num_instances = real_num_instances
            elif line.startswith(b'Warning: number of transformation instances exceeded'):
                # TODO: report?
                pass

    def transform(
        self,
        test_case: Path,
        state,
        process_event_notifier,
        original_test_case=None,
        written_paths: set[Path] | None = None,
        *args,
        **kwargs,
    ):
        logging.debug(f'TRANSFORM: {state}')

        if isinstance(state, DirBinaryState):
            sources = sources_of(test_case)
            if state.file_index >= len(sources):
                return (PassResult.STOP, state)
            target = sources[state.file_index]
            key = target
            if original_test_case is not None:
                key = Path(original_test_case) / target.relative_to(test_case)
            inner = state.inner
        else:
            target = test_case
            key = original_test_case if original_test_case is not None else test_case
            inner = state

        args = [
            f'--transformation={self.arg}',
            f'--counter={inner.index + 1}',
            f'--to-counter={inner.end()}',
            '--warn-on-counter-out-of-bounds',
            '--report-instances-count',
        ]
        if not self._compilation_database:
            args.append(f'--std={inner.clang_delta_std}')
        args += self._compilation_database_args(key)
        if self._clang_delta_preserve_routine:
            args.append(f'--preserve-routine="{self._clang_delta_preserve_routine}"')
        prog = self.external_programs['clang_delta']
        assert prog
        cmd = [prog] + args + [str(target)]
        logging.debug(' '.join(cmd))

        stdout, stderr, returncode = process_event_notifier.run_process(cmd)
        self.parse_stderr(inner, stderr)
        match returncode:
            case 0:
                # An empty result is not a reduction, it is a lost file.
                if not stdout:
                    return (PassResult.STOP, state)
                target.write_bytes(stdout)
                if written_paths is not None:
                    # Whatever is not declared here is deleted from the test
                    # case afterwards, so the whole tree has to be declared.
                    written_paths.update(test_case.rglob('*') if test_case.is_dir() else [test_case])
                return (PassResult.OK, state)
            case 255:
                return (PassResult.STOP, state)
            case _:
                return (PassResult.ERROR, state)


def attach_clang_delta_std(state, std):
    if state is None:
        return None
    state.clang_delta_std = std
    return state
