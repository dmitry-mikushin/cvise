"""Keeping what clang_delta said, so a restart need not ask again.

The defect these are about, MEASURED on a 5.9 h run of ns-projection: a full
initialisation of one clang_delta transformation walks 1158 files and takes
3.4 h; a reduction was accepted every 7m18s, and every acceptance sends an
unfinished initialisation back to the first file. So 3.5% of the work fitted
between two resets, four transformations burned 23.1 machine-hours at 100% CPU,
and ClangHintsPass contributed to none of the 173 accepted reductions of two
runs.

What makes a cache the answer rather than a speed-up: a publication changes a
handful of files out of 1158, and what clang_delta said about the other
thousand still holds.
"""

import os
from pathlib import Path

import pytest

from cvise.utils.hintcache import HintCache, fingerprint, key_for


class TestTheKey:
    def test_the_same_question_gets_the_same_name(self):
        assert key_for(b'content', b'pass') == key_for(b'content', b'pass')

    def test_a_changed_file_is_a_different_question(self):
        assert key_for(b'content', b'pass') != key_for(b'content!', b'pass')

    def test_a_different_transformation_is_a_different_question(self):
        assert key_for(b'content', b'a') != key_for(b'content', b'b')

    def test_the_parts_cannot_run_together(self):
        """Without length prefixes ('ab','c') and ('a','bc') are one question.

        Which would mean a transformation name and a C++ standard could answer
        for each other -- the kind of collision that returns plausible hints
        for the wrong thing rather than failing.
        """
        assert key_for(b'ab', b'c') != key_for(b'a', b'bc')

    def test_it_is_a_usable_filename(self):
        key = key_for(b'x')
        assert key.isalnum() and len(key) == 32


class TestWhatItStores:
    def test_what_went_in_comes_out(self, tmp_path):
        cache = HintCache(tmp_path)
        cache.put('abcd1234', b'the hints')
        assert cache.get('abcd1234') == b'the hints'

    def test_an_unknown_key_is_a_miss_and_not_an_error(self, tmp_path):
        assert HintCache(tmp_path).get('0' * 32) is None

    def test_it_counts_what_it_saved(self, tmp_path):
        cache = HintCache(tmp_path)
        cache.get('a' * 32)
        cache.put('a' * 32, b'x')
        cache.get('a' * 32)
        assert (cache.hits, cache.misses) == (1, 1)

    def test_empty_output_is_a_hit_and_not_a_miss(self, tmp_path):
        """clang_delta legitimately finds nothing, and that answer is worth
        keeping -- it cost the same seconds as any other."""
        cache = HintCache(tmp_path)
        cache.put('b' * 32, b'')
        assert cache.get('b' * 32) == b''

    def test_a_second_answer_to_the_same_question_replaces_the_first(self, tmp_path):
        cache = HintCache(tmp_path)
        cache.put('c' * 32, b'one')
        cache.put('c' * 32, b'two')
        assert cache.get('c' * 32) == b'two'


class TestWhenItCannot:
    def test_an_unwritable_root_costs_a_warning_and_not_the_run(self, tmp_path, caplog):
        """The cache is a speed-up. Losing it must cost only speed."""
        blocked = tmp_path / 'file'
        blocked.write_text('not a directory')
        cache = HintCache(blocked / 'cache')
        assert cache.root is None
        cache.put('d' * 32, b'x')
        assert cache.get('d' * 32) is None
        assert 'hints will not be cached' in caplog.text

    def test_a_store_that_fails_is_silent_and_harmless(self, tmp_path):
        cache = HintCache(tmp_path)
        cache.root = tmp_path / 'gone'
        (tmp_path / 'gone').write_text('not a directory')
        cache.put('e' * 32, b'x')  # must not raise

    def test_nothing_is_left_behind_by_a_store(self, tmp_path):
        """A half-written answer read as a hint bundle is silent corruption,
        so it is written elsewhere and renamed into place."""
        cache = HintCache(tmp_path)
        cache.put('f' * 32, b'x')
        leftovers = [p for p in tmp_path.rglob('.tmp*')]
        assert not leftovers, leftovers


class TestTheFingerprint:
    def test_it_changes_when_the_file_does(self, tmp_path):
        path = tmp_path / 'tool'
        path.write_bytes(b'v1')
        before = fingerprint(path)
        os.utime(path, (0, 0))
        path.write_bytes(b'v2 is longer')
        assert fingerprint(path) != before

    def test_a_missing_file_has_one_too(self, tmp_path):
        assert fingerprint(tmp_path / 'nothing') == b'missing'


class TestThePassUsesIt:
    """That the wiring is there, which the arithmetic above cannot say."""

    def a_pass(self, tmp_path, calls):
        from cvise.passes.clanghints import ClangHintsPass

        p = ClangHintsPass(arg='callexpr-to-value',
                           external_programs={'clang_delta': str(tmp_path / 'tool')})
        (tmp_path / 'tool').write_bytes(b'pretend binary')
        p._cache = HintCache(tmp_path / 'cache')

        class Notifier:
            def run_process(self, cmd, timeout=None):
                calls.append(cmd)
                return b'0\n', b'', 0

        return p, Notifier()

    def test_the_second_ask_about_one_file_does_not_run_clang_delta(self, tmp_path):
        calls = []
        p, notifier = self.a_pass(tmp_path, calls)
        source = tmp_path / 'a.cpp'
        source.write_text('int f();\n')

        p._generate_hints_for_file(source, None, 100, notifier)
        assert len(calls) == 1, 'the first ask must run the tool'
        p._generate_hints_for_file(source, None, 100, notifier)
        assert len(calls) == 1, 'the second ask ran it again; the cache is not wired in'

    def test_a_changed_file_is_asked_about_again(self, tmp_path):
        calls = []
        p, notifier = self.a_pass(tmp_path, calls)
        source = tmp_path / 'a.cpp'
        source.write_text('int f();\n')
        p._generate_hints_for_file(source, None, 100, notifier)
        source.write_text('int f(); int g();\n')
        p._generate_hints_for_file(source, None, 100, notifier)
        assert len(calls) == 2, 'a changed file was answered from the cache'

    def test_two_files_with_the_same_content_are_one_question(self, tmp_path):
        """Which is why the key is the content and not the path: a reduction
        makes identical files under different names constantly."""
        calls = []
        p, notifier = self.a_pass(tmp_path, calls)
        for name in ('a.cpp', 'b.cpp'):
            (tmp_path / name).write_text('int f();\n')
            p._generate_hints_for_file(tmp_path / name, None, 100, notifier)
        assert len(calls) == 1

    def test_a_crash_is_not_remembered(self, tmp_path):
        """MEASURED: callexpr-to-value segfaults on 6 of 24 translation units of
        this project. Caching that would make a bug being fixed look permanent."""
        from cvise.passes.clanghints import ClangDeltaError

        calls = []
        p, _ = self.a_pass(tmp_path, calls)
        source = tmp_path / 'a.cpp'
        source.write_text('int f();\n')

        class Crashing:
            def run_process(self, cmd, timeout=None):
                calls.append(cmd)
                return b'', b'', 139

        for _ in range(2):
            with pytest.raises(ClangDeltaError):
                p._generate_hints_for_file(source, None, 100, Crashing())
        assert len(calls) == 2, 'a crash was cached'

    def test_a_different_transformation_is_asked_separately(self, tmp_path):
        calls = []
        p, notifier = self.a_pass(tmp_path, calls)
        source = tmp_path / 'a.cpp'
        source.write_text('int f();\n')
        p._generate_hints_for_file(source, None, 100, notifier)
        p.arg = 'remove-unused-function'
        p._identity = None
        p._generate_hints_for_file(source, None, 100, notifier)
        assert len(calls) == 2

    def test_a_rebuilt_clang_delta_invalidates_everything(self, tmp_path):
        """Its answers are its own; a new build may give different ones."""
        calls = []
        p, notifier = self.a_pass(tmp_path, calls)
        source = tmp_path / 'a.cpp'
        source.write_text('int f();\n')
        p._generate_hints_for_file(source, None, 100, notifier)
        (tmp_path / 'tool').write_bytes(b'a different, longer binary')
        p._identity = None
        p._generate_hints_for_file(source, None, 100, notifier)
        assert len(calls) == 2
