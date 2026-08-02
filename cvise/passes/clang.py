import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cvise.passes.abstract import AbstractPass, PassResult

# clang_delta transforms one translation unit, so a directory is reduced by
# walking the units it contains; a header is reached through the units that
# include it.
SOURCE_SUFFIXES = ('.c', '.cc', '.cp', '.cpp', '.cxx', '.c++', '.C', '.m', '.mm', '.cl', '.cu', '.hip')


@dataclass(frozen=True)
class DirState:
    """Position inside a directory test case: which unit, and which instance in it.

    A single counter cannot address a directory, so it is decomposed into the
    file to transform and the instance number within that file.

    The number of instances that file has is part of the position, and not for
    convenience: without it nothing can tell when a unit is finished. The pass
    used to walk on to the next unit inside the job and start it again at
    instance 1, which the scheduler never saw -- so it kept raising a counter
    that addressed nothing while the same first instance of the next unit was
    produced over and over. A reduction that reached this pass did not end.
    """

    file_index: int
    counter: int
    instances: int

    def __repr__(self):
        return f'DirState(file #{self.file_index}, instance {self.counter} of {self.instances})'


def sources_of(test_case: Path) -> list[Path]:
    return sorted(p for p in test_case.rglob('*') if p.is_file() and p.suffix in SOURCE_SUFFIXES)


class ClangPass(AbstractPass):
    def __init__(
        self,
        arg: str,
        external_programs: dict[str, str | None],
        user_clang_delta_std: str | None = None,
        compilation_database: str | None = None,
        **kwargs,
    ):
        super().__init__(
            arg=arg,
            external_programs=external_programs,
            user_clang_delta_std=user_clang_delta_std,
            compilation_database=compilation_database,
            **kwargs,
        )
        self._user_clang_delta_std = user_clang_delta_std
        self._compilation_database = compilation_database

    def check_prerequisites(self):
        return self.check_external_program('clang_delta')

    def supports_dir_test_cases(self) -> bool:
        return True

    def count_instances(self, target: Path) -> int:
        """How many of this transformation the unit offers, asked of clang_delta.

        Counting happens where the test case is, not in a job's copy of it, so
        the file is its own key: that is the path the database names.
        """
        args = [self.external_programs['clang_delta'], f'--query-instances={self.arg}']
        if self._user_clang_delta_std and not self._compilation_database:
            args.append(f'--std={self._user_clang_delta_std}')
        if self._compilation_database:
            args.append(f'--compilation-database={self._compilation_database}')
            args.append(f'--compilation-database-key={target.resolve()}')
        try:
            proc = subprocess.run(args + [str(target)], capture_output=True, text=True)
        except OSError:
            return 0
        if proc.returncode != 0:
            # A unit clang_delta will not parse offers nothing; that is not a
            # reason to abandon the units after it.
            logging.debug('cannot count %s instances in %s: %s', self.arg, target, proc.stderr.strip()[:200])
            return 0
        m = re.match('Available transformation instances: ([0-9]+)$', proc.stdout.strip())
        return int(m.group(1)) if m else 0

    def _state_from_file(self, sources, file_index: int):
        """First unit at or after file_index that has anything to transform."""
        while file_index < len(sources):
            instances = self.count_instances(sources[file_index])
            if instances > 0:
                return DirState(file_index=file_index, counter=1, instances=instances)
            file_index += 1
        return None

    def new(self, test_case: Path, *args, **kwargs):
        if test_case.is_dir():
            sources = sources_of(test_case)
            if not sources:
                return None
            return self._state_from_file(sources, 0)
        return 1

    def advance(self, test_case: Path, state):
        if isinstance(state, DirState):
            if state.counter < state.instances:
                return DirState(
                    file_index=state.file_index,
                    counter=state.counter + 1,
                    instances=state.instances,
                )
            # This unit is done; the pass is not.
            return self._state_from_file(sources_of(test_case), state.file_index + 1)
        return state + 1

    def advance_on_success(self, test_case: Path, state, *args, **kwargs):
        if isinstance(state, DirState):
            # A successful transformation changes how many are left, so the
            # count has to be asked again rather than assumed to be one fewer.
            sources = sources_of(test_case)
            if state.file_index >= len(sources):
                return None
            instances = self.count_instances(sources[state.file_index])
            if state.counter <= instances:
                return DirState(
                    file_index=state.file_index, counter=state.counter, instances=instances
                )
            return self._state_from_file(sources, state.file_index + 1)
        return state

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
        if not isinstance(state, DirState):
            result, _ = self._transform_file(test_case, state, process_event_notifier, original_test_case)
            return (result, state)

        sources = sources_of(test_case)
        if state.file_index >= len(sources):
            return (PassResult.STOP, state)
        target = sources[state.file_index]
        key = self._lookup_key(test_case, target, original_test_case)
        result, _ = self._transform_file(target, state.counter, process_event_notifier, key)
        if result == PassResult.OK and written_paths is not None:
            # Everything not declared here is deleted from the test case as
            # extraneous, so the whole tree has to be declared, not just the
            # file this transformation rewrote.
            written_paths.update(test_case.rglob('*'))
        return (result, state)

    def _transform_file(self, target: Path, counter: int, process_event_notifier, lookup_key):
        args = [
            self.external_programs['clang_delta'],
            f'--transformation={self.arg}',
            f'--counter={counter}',
        ]
        if self._user_clang_delta_std and not self._compilation_database:
            args.append(f'--std={self._user_clang_delta_std}')
        if self._compilation_database:
            args.append(f'--compilation-database={self._compilation_database}')
            # The pass works on a copy in a scratch directory, so the flags have
            # to be looked up under the path the build system knows.
            if lookup_key is None:
                raise ValueError('a compilation database needs the original path of the test case')
            args.append(f'--compilation-database-key={Path(lookup_key).resolve()}')
        cmd = args + [str(target)]

        logging.debug(' '.join(cmd))

        stdout, _, returncode = process_event_notifier.run_process(cmd)
        match returncode:
            case 0:
                # An empty result is not a reduction, it is a lost file; never
                # let it reach the test case.
                if not stdout:
                    return (PassResult.STOP, counter)
                target.write_bytes(stdout)
                return (PassResult.OK, counter)
            case 1 | 255:
                return (PassResult.STOP, counter)
            case _:
                return (PassResult.ERROR, counter)
