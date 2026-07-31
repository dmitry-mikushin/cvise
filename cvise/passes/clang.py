import logging
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
    """

    file_index: int
    counter: int

    def __repr__(self):
        return f'DirState(file #{self.file_index}, instance {self.counter})'


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

    def new(self, test_case: Path, *args, **kwargs):
        if test_case.is_dir():
            return DirState(file_index=0, counter=1) if sources_of(test_case) else None
        return 1

    def advance(self, test_case: Path, state):
        if isinstance(state, DirState):
            return DirState(file_index=state.file_index, counter=state.counter + 1)
        return state + 1

    def advance_on_success(self, test_case: Path, state, *args, **kwargs):
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

        # Running out of instances in one unit does not end the pass for the
        # whole directory, so walk on to the next one.
        sources = sources_of(test_case)
        while state.file_index < len(sources):
            target = sources[state.file_index]
            key = None
            if original_test_case is not None:
                key = Path(original_test_case) / target.relative_to(test_case)
            result, _ = self._transform_file(target, state.counter, process_event_notifier, key)
            if result == PassResult.OK and written_paths is not None:
                # Everything not declared here is deleted from the test case as
                # extraneous, so the whole tree has to be declared, not just the
                # file this transformation rewrote.
                written_paths.update(test_case.rglob('*'))
            if result in (PassResult.OK, PassResult.ERROR):
                return (result, state)
            state = DirState(file_index=state.file_index + 1, counter=1)
        return (PassResult.STOP, state)

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
