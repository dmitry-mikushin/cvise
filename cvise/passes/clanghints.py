import dataclasses
import logging
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import msgspec

from cvise.passes.abstract import BinaryState, SubsegmentState
from cvise.passes.clang import SOURCE_SUFFIXES
from cvise.passes.hint_based import HintBasedPass, HintState
from cvise.utils.hint import Hint, HintBundle
from cvise.utils.hintcache import HintCache, fingerprint, key_for
from cvise.utils.process import ProcessEventNotifier

CLANG_STD_CHOICES = ('c++98', 'c++11', 'c++14', 'c++17', 'c++20', 'c++2b')


@dataclass(frozen=True, slots=True)
class ClangState(HintState):
    """Extends HintState to store additional information needed by ClangHintsPass.

    See the comment in ClangHintsPass for the background."""

    clang_std: str | None

    @staticmethod
    def wrap(parent: HintState | None, clang_std: str | None) -> HintState | None:
        if parent is None:
            return None
        attrs = {f.name: getattr(parent, f.name) for f in dataclasses.fields(parent) if hasattr(parent, f.name)}
        return ClangState(clang_std=clang_std, **attrs)


class ClangDeltaError(Exception):
    pass


class ClangHintsPass(HintBasedPass):
    """A pass that performs reduction using the hints produced by the clang_delta tool.

    Implementation-wise, we don't use default new/advance/advance_on_success implementation from the base class, because
    we want to brute-force Clang's `--std=` parameter that maximizes the generated set of hints (unless "iterate_stds"
    is False). This requires having special logic in new() and carrying over some extra information from new() to
    advance_on_success() throughout all advance() calls.

    Strategy by default is "binsearch" - trying all instances first, then the first half, then the second, etc. Another
    supported strategy is "onebyone" - attempting each instance, starting from a random one, individually.
    """

    def __init__(
        self,
        arg: str,
        external_programs: dict[str, str | None],
        user_clang_delta_std: str | None = None,
        compilation_database: str | None = None,
        strategy: str | None = None,
        iterate_stds: bool | None = None,
        **kwargs,
    ):
        self._compilation_database = compilation_database
        super().__init__(
            arg=arg, external_programs=external_programs, user_clang_delta_std=user_clang_delta_std, **kwargs
        )
        self._user_clang_delta_std = user_clang_delta_std
        self._strategy = strategy
        self._iterate_stds = False if iterate_stds is None else iterate_stds
        # What clang_delta already answered, so that an initialisation cut
        # short does not have to start from the first file again.
        self._cache = HintCache()
        self._identity: bytes | None = None

    def check_prerequisites(self):
        return self.check_external_program('clang_delta')

    def new(
        self, test_case: Path, tmp_dir: Path, job_timeout, process_event_notifier: ProcessEventNotifier, *args, **kwargs
    ):
        # If configured accordingly, choose the best standard unless the user provided one.
        if self._compilation_database:
            # The build already says which standard each file is compiled with,
            # so there is nothing to guess and nothing to brute-force.
            std_choices = [None]
        elif self._user_clang_delta_std:
            std_choices = [self._user_clang_delta_std]
        elif self._iterate_stds:
            std_choices = CLANG_STD_CHOICES
        else:
            std_choices = [None]  # denotes not specifying "--std=" at all

        best_std = None
        best_bundle: HintBundle | None = None
        last_error: ClangDeltaError | None = None
        for std in std_choices:
            start = time.monotonic()
            try:
                bundle = self._generate_hints_for_standard(test_case, std, job_timeout, process_event_notifier)
            except ClangDeltaError as e:
                last_error = e
                continue
            took = time.monotonic() - start
            # prefer newer standard if the # of instances is equal
            if best_bundle is None or len(bundle.hints) >= len(best_bundle.hints):
                best_std = std
                best_bundle = bundle
            logging.debug(
                'available transformation opportunities for %s: %d, took: %.2f s' % (std, len(bundle.hints), took)
            )

        if best_bundle is None:
            logging.debug('%s', last_error)
            return None

        if best_std:
            logging.debug(
                'clang_delta %s using C++ standard: %s with %d transformation opportunities',
                self.arg,
                best_std,
                len(best_bundle.hints),
            )
        else:
            logging.debug(
                'clang_delta %s: %d transformation opportunities',
                self.arg,
                len(best_bundle.hints),
            )
        # Let the parent class complete the initialization, but create our own state to remember the chosen standard.
        hint_state = self.new_from_hints(best_bundle, tmp_dir)
        return ClangState.wrap(hint_state, best_std)

    def advance(self, test_case: Path, state):
        new_state = super().advance(test_case, state)
        # Re-attach the remembered standard.
        return ClangState.wrap(new_state, state.clang_std)

    def advance_on_success(
        self,
        test_case: Path,
        state,
        new_tmp_dir: Path,
        job_timeout: int,
        process_event_notifier: ProcessEventNotifier,
        *args,
        **kwargs,
    ):
        # Keep using the same standard as the one chosen in new() - repeating the choose procedure on every successful
        # reduction would be too costly.
        try:
            hints = self._generate_hints_for_standard(test_case, state.clang_std, job_timeout, process_event_notifier)
        except ClangDeltaError as e:
            logging.warning('%s', e)
            return None
        new_state = self.advance_on_success_from_hints(hints, state, new_tmp_dir)
        return ClangState.wrap(new_state, state.clang_std)

    def create_elementary_state(self, hint_count: int) -> BinaryState | SubsegmentState | None:
        match self._strategy:
            case 'binsearch' | None:  # default strategy
                return BinaryState.create(instances=hint_count)
            case 'onebyone':
                return SubsegmentState.create(instances=hint_count, min_chunk=1, max_chunk=1)
            case _:
                raise ValueError(f'Unexpected strategy: {self._strategy}')

    def supports_dir_test_cases(self) -> bool:
        # A directory is reduced by running clang_delta over each translation
        # unit in it and merging what comes back into one bundle.
        return True

    def _generate_hints_for_standard(
        self, test_case: Path, std: str | None, timeout: int, process_event_notifier: ProcessEventNotifier
    ) -> HintBundle:
        if not test_case.is_dir():
            return self._generate_hints_for_file(test_case, std, timeout, process_event_notifier)

        sources = sorted(p for p in test_case.rglob('*') if p.is_file() and p.suffix in SOURCE_SUFFIXES)
        vocabulary: list[bytes] = []
        hints: list[Hint] = []
        failures = []
        for source in sources:
            try:
                bundle = self._generate_hints_for_file(source, std, timeout, process_event_notifier)
            except ClangDeltaError as e:
                # One unhandled unit must not cost us the whole directory.
                failures.append(f'{source}: {e}')
                continue
            offset = len(vocabulary)
            vocabulary += bundle.vocabulary
            path_id = len(vocabulary)
            vocabulary.append(str(source.relative_to(test_case)).encode())
            for hint in bundle.hints:
                patches = tuple(
                    msgspec.structs.replace(
                        patch,
                        path=path_id if patch.path is None else patch.path + offset,
                        value=None if patch.value is None else patch.value + offset,
                    )
                    for patch in hint.patches
                )
                hints.append(
                    msgspec.structs.replace(
                        hint,
                        patches=patches,
                        type=None if hint.type is None else hint.type + offset,
                        extra=None if hint.extra is None else hint.extra + offset,
                    )
                )
        if failures and not hints:
            raise ClangDeltaError('; '.join(failures))
        for f in failures:
            logging.debug('clang_delta skipped a file: %s', f)
        return HintBundle(vocabulary=vocabulary, hints=hints)

    def _tool_identity(self) -> bytes:
        """What, besides the file, decides clang_delta's answer.

        The tool itself and the compilation database, both by size and mtime
        rather than by content: the database is megabytes and is read on every
        file, and clang_delta is a 32 MB binary. Computed once per pass.

        The database is taken whole rather than per-file. A finer key would
        have to parse it for every file, which is the cost this is avoiding;
        and it changes only when the project is reconfigured, so the coarse
        key throws the cache away exactly when it deserves to be thrown away.
        """
        if self._identity is None:
            parts = [fingerprint(Path(self.external_programs['clang_delta'] or ''))]
            if self._compilation_database:
                parts.append(fingerprint(Path(self._compilation_database)))
            self._identity = b'|'.join(parts)
        return self._identity

    def _generate_hints_for_file(
        self, test_case: Path, std: str | None, timeout: int, process_event_notifier: ProcessEventNotifier
    ) -> HintBundle:
        options = [f'--transformation={self.arg}', '--generate-hints']
        if std is not None:
            options.append(f'--std={std}')
        if self._compilation_database:
            options.append(f'--compilation-database={self._compilation_database}')
            # Hints are generated from a copy in a scratch directory, so the
            # flags are looked up under the path the build system knows.
            options.append(f'--compilation-database-key={Path(test_case).resolve()}')

        prog = self.external_programs['clang_delta']
        assert prog is not None

        cmd = [prog] + options + [str(test_case)]
        logging.debug(shlex.join(str(s) for s in cmd))

        # Everything that decides the answer, and nothing that does not. The
        # file's CONTENT and not its path: a reduction produces identical files
        # under different names constantly, and they have identical hints.
        try:
            content = test_case.read_bytes()
        except OSError as e:
            raise ClangDeltaError(f'cannot read {test_case}: {e}') from e
        key = key_for(content, self.arg.encode(), (std or '').encode(), self._tool_identity())
        cached = self._cache.get(key)
        if cached is not None:
            return parse_clang_delta_hints(cached)

        try:
            stdout, stderr, returncode = process_event_notifier.run_process(cmd, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise ClangDeltaError(f'clang_delta ({" ".join(options)}) {timeout}s timeout reached') from e
        except subprocess.SubprocessError as e:
            raise ClangDeltaError(f'clang_delta ({" ".join(options)}) failed: {e}') from e

        if returncode != 0:
            stderr = stderr.decode('utf-8', 'ignore').strip()
            delim = ': ' if stderr else ''
            raise ClangDeltaError(
                f'clang_delta ({" ".join(options)}) failed with exit code {returncode}{delim}{stderr}'
            )
        # Only a success is remembered. A failure may be the file's fault and
        # may be the tool's -- MEASURED, callexpr-to-value segfaults on 6 of 24
        # translation units of this project -- and caching a crash would make a
        # bug that is being fixed look permanent.
        self._cache.put(key, stdout)
        return parse_clang_delta_hints(stdout)


def parse_clang_delta_hints(stdout: bytes) -> HintBundle:
    # When reading, gracefully handle EOF because the tool might've failed with no output.
    if not stdout.strip():
        return HintBundle(hints=[])
    stdout_view = memoryview(stdout)

    # Read vocabulary: size, newline, zero-separated string list.
    pos = stdout.index(b'\n')
    vocab_size = int(stdout_view[:pos])
    pos += 1
    vocab = []
    for _ in range(vocab_size):
        end = stdout.index(0, pos)
        vocab.append(bytes(stdout_view[pos:end]))
        pos = end + 1

    # Read hints.
    hints = []
    hint_decoder = msgspec.json.Decoder(type=Hint)
    while pos < len(stdout):
        end = stdout.index(b'\n', pos)
        hints.append(hint_decoder.decode(stdout_view[pos:end]))
        pos = end + 1

    return HintBundle(vocabulary=vocab, hints=hints)
