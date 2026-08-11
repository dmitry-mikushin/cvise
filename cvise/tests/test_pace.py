"""Knowing when to stop, graded on the run that did not.

MEASURED on ns-projection: one run of 10.96 h spent 6.00 h -- 54.8% of itself
-- after its last accepted reduction, and ended only when five passes had each
burned 50 000 jobs. That is the defect these tests are about, and the sequences
below are that run's real intervals, not invented ones.

The hard part is not detecting a dead run. It is detecting one without cutting
a live one short, so every test that shows the rule firing is paired with one
showing it holding its nerve.
"""

import logging
import time
from pathlib import Path

import pytest

from cvise.utils import pace as pace_module
from cvise.utils.pace import Pace, Series, report

# The real intervals between accepted reductions, in seconds, from the three
# runs of an hour or more that ns-projection produced, with what the tree
# measured at each point and how long each run went on after its last one.
#
# Run 1 is the pathological one: note the tail of 30 s intervals -- a flurry of
# tiny reductions -- and then 21 610 s of nothing.
HEALTHY_A = dict(
    gaps=[708, 806, 1240, 832, 905, 880, 1291, 646, 775, 712, 629, 512, 692, 619,
          814, 716, 776, 1561, 1397, 1322, 1236],
    lines=[98140, 98083, 97948, 97569, 97555, 97131, 94913, 94855, 94561, 94169,
           93068, 93005, 89742, 89026, 88650, 88336, 88324, 88299, 88073, 86419,
           82534, 82443],
    tail=514,
)
DEAD = dict(
    gaps=[423, 556, 785, 412, 592, 588, 923, 927, 1265, 617, 870, 762, 1021, 1129,
          685, 733, 1377, 1098, 822, 1038, 108, 86, 106, 81, 317, 136, 39, 174, 32, 33],
    lines=[81497, 77945, 70535, 68099, 67166, 65155, 62051, 59941, 55722, 53315,
           48889, 47903, 45351, 43765, 41258, 40456, 39785, 36831, 35521, 35113,
           34870, 34686, 34555, 34552, 34429, 34429, 34429, 34426, 34420, 34419, 34418],
    tail=21610,
)
HEALTHY_B = dict(
    gaps=[686, 680, 3991, 632, 594, 2685, 587, 546, 566, 746, 679, 663, 596, 601,
          715, 700, 536, 582, 562, 691],
    lines=[154085, 152127, 150901, 150901, 147654, 147301, 147301, 146946, 146807,
           146180, 145747, 145579, 144627, 143996, 143081, 141598, 139674, 139361,
           138158, 137235, 136564],
    tail=111,
)
RUNS = {'healthy-a': HEALTHY_A, 'the one that died': DEAD, 'healthy-b': HEALTHY_B}


def replay(run, patience=None, upto=None):
    """Feed a run's real history to a Pace, and hand back where it ended up."""
    p = Pace(patience=patience or pace_module.PATIENCE, started=0.0)
    now = 0.0
    p.record(run['lines'][0], now=now)
    for gap, lines in zip(run['gaps'][:upto], run['lines'][1:]):
        now += gap
        p.record(lines, now=now)
    return p, now


class TestTheRuleOnTheRunsThatHappened:
    @pytest.mark.parametrize('name', ['healthy-a', 'healthy-b'])
    def test_a_run_that_was_still_working_is_not_cut_short(self, name):
        """At every moment it lived through, the answer must be "keep going".

        Asked at every point rather than only at the end, because a rule that
        is right about the finished run and wrong halfway through it would have
        thrown away everything that came after halfway.
        """
        run = RUNS[name]
        for upto in range(len(run['gaps']) + 1):
            p, now = replay(run, upto=upto)
            assert not p.spent(now), f'called finished after {upto} of its reductions'

    def test_the_run_that_died_is_called_before_it_wastes_hours(self):
        p, now = replay(DEAD)
        deadline = p.deadline()
        assert deadline is not None
        waited = deadline - now
        assert waited < DEAD['tail'], 'the rule would have waited as long as the run did'
        saved = (DEAD['tail'] - waited) / 3600
        assert saved > 4, f'only {saved:.1f} h of the 6.0 h would have been saved'

    def test_nothing_that_was_found_would_have_been_lost(self):
        """The rule may only fire after the last thing this run ever produced.

        The expensive direction to be wrong in: time spent waiting is time, and
        a reduction thrown away is work that has to be done again from a worse
        starting point.
        """
        for name, run in RUNS.items():
            for upto in range(len(run['gaps'])):
                p, now = replay(run, upto=upto)
                deadline = p.deadline()
                if deadline is None:
                    continue
                next_one = now + run['gaps'][upto]
                assert deadline >= next_one or not p.spent(next_one), (
                    f'{name}: would have stopped {next_one - deadline:.0f} s before '
                    f'a reduction that did arrive'
                )


class TestWhatItRefusesToSay:
    def test_three_is_where_the_loss_begins_and_the_setting_must_stay_above_it(self):
        """The lower boundary, asserted rather than remembered.

        MEASURED: a patience of 3 stops one of these runs 10 737 lines before it
        was done, and every setting from 4 upwards stops none of them. Anyone
        lowering PATIENCE towards that cliff should have to delete this.
        """
        reckless = []
        for name, run in RUNS.items():
            for upto in range(len(run['gaps'])):
                p, now = replay(run, patience=3, upto=upto)
                if p.deadline() is not None and p.spent(now + run['gaps'][upto]):
                    reckless.append(name)
                    break
        assert reckless, ('a patience of 3 no longer cuts any of these runs short, '
                          'so the margin PATIENCE is chosen for is no longer measured '
                          'by anything')
        assert pace_module.PATIENCE >= 4, 'PATIENCE is at or below the measured cliff'

    def test_a_run_that_has_found_nothing_at_all_still_refuses(self):
        """Zero intervals is zero information, and there is nothing to scale."""
        p = Pace(started=0.0)
        p.record(1000, now=0)
        assert p.usual is None
        assert p.deadline() is None
        assert not p.spent(now=100000)
        assert 'intervals needed' in report(p, now=120)

    def test_one_interval_is_thin_evidence_and_buys_much_more_patience(self):
        p = Pace(started=0.0)
        p.record(1000, now=0)
        p.record(900, now=60)
        assert p.usual == 60
        assert p.patience_now == pace_module.PATIENCE * pace_module.WARMUP
        assert not p.spent(now=60 + 60 * p.patience_now - 1)
        assert p.spent(now=60 + 60 * p.patience_now + 1)

    def test_patience_shrinks_to_the_measured_figure_as_evidence_arrives(self):
        p = Pace(started=0.0)
        p.record(1000, now=0)
        seen = []
        for i in range(1, pace_module.WARMUP + 3):
            p.record(1000 - i, now=i * 60)
            seen.append(p.patience_now)
        assert seen == sorted(seen, reverse=True), seen
        assert seen[-1] == pace_module.PATIENCE

    def test_the_run_that_could_never_be_stopped(self):
        """The defect this scaling exists for, on the run that suffered it.

        MEASURED on a real reduction of llama.cpp: one accepted reduction after
        281 s and then ELEVEN HOURS of nothing, ending in a crash rather than a
        decision. One interval is fewer than WARMUP, so `usual` was None,
        `deadline()` was None, and `spent()` stayed False for ever -- the run
        that most needed stopping was precisely the one that could not be.
        """
        p = Pace(started=0.0)
        p.record(138896, now=1786385262.0)
        p.record(128924, now=1786385543.0)
        deadline = p.deadline()
        assert deadline is not None, 'still cannot be stopped'
        waited = (deadline - 1786385543.0) / 3600
        assert 1 < waited < 6, f'gives up after {waited:.1f} h, which is not a sane bound'
        assert p.spent(now=1786385543.0 + 11 * 3600), 'eleven hours of silence was not enough'

    def test_a_run_that_has_never_succeeded_is_never_called_finished(self):
        """The first candidate can take longer than everything after it: the
        project has to build once before anything is judged at all."""
        p = Pace(started=0.0)
        assert not p.spent(now=10 * 3600)
        assert p.deadline() is None


class TestTheLine:
    def test_it_names_a_pace_a_rate_and_a_deadline(self):
        p, now = replay(HEALTHY_A)
        line = report(p, now=now + 60)
        assert 'a reduction every' in line
        assert 'lines/h' in line
        assert 'giving up in' in line

    def test_a_silent_run_stops_promising_a_next_one(self):
        p, now = replay(DEAD)
        line = report(p, now=now + DEAD['tail'])
        assert 'silent for' in line
        assert 'giving up in' not in line

    def test_the_rate_is_lines_and_not_bytes(self):
        p, now = replay(HEALTHY_A)
        # 98140 -> 82443 over 20 923 s is a shade under 2700 lines an hour.
        rate = p.per_hour(now)
        assert 2000 < rate < 3500, rate


class TestTheSeries:
    def test_a_point_carries_an_absolute_time(self, tmp_path):
        """The whole reason the file exists.

        The log line has a relative clock, so three consecutive runs in one log
        are indistinguishable from one and no series can be reconstructed from
        it. Establishing the numbers these tests are built on meant ordering
        segments by file count and hoping.
        """
        path = tmp_path / 'progress.tsv'
        s = Series(path)
        s.add(1000, 100, 10, 'LinesPass::0')
        point = next(line for line in path.read_text().splitlines()
                     if not line.startswith('#'))
        when, byts, lines, files, via = point.split('\t')
        assert int(when) > 1_700_000_000, 'not a wall-clock time'
        assert (byts, lines, files, via) == ('1000', '100', '10', 'LinesPass::0')

    def test_each_run_says_where_it_begins(self, tmp_path):
        """So a reader never has to guess where one run ends and the next starts.

        Guessing would mean guessing by the size of the interval, and the
        longest gap ever observed WITHIN a run is 66 min -- not comfortably
        below how quickly a run can be restarted.
        """
        path = tmp_path / 'progress.tsv'
        Series(path).add(1000, 100, 10, 'a')
        Series(path).add(900, 90, 9, 'b')
        marks = [line for line in path.read_text().splitlines()
                 if line.startswith('#run')]
        assert len(marks) == 2
        assert int(marks[0].split('\t')[1]) > 1_700_000_000

    def test_a_second_run_appends_rather_than_starting_again(self, tmp_path):
        path = tmp_path / 'progress.tsv'
        Series(path).add(1000, 100, 10, 'a')
        Series(path).add(900, 90, 9, 'b')
        body = path.read_text().splitlines()
        assert body[0].startswith('#when')
        assert len([line for line in body if not line.startswith('#')]) == 2

    def test_an_unwritable_place_costs_a_warning_and_not_the_run(self, tmp_path, caplog):
        """A record of the run is not a dependency of it."""
        s = Series(tmp_path / 'nonexistent' / 'progress.tsv')
        s.add(1000, 100, 10, 'a')
        assert s.path is None
        assert 'progress will not be recorded' in caplog.text

    def test_no_path_is_not_an_error(self):
        Series(None).add(1000, 100, 10, 'a')


class TestTheWiring:
    """That the decision reaches the machinery, which the arithmetic cannot say.

    The stop is made by marking every pass defunct rather than by raising,
    because that is how a sweep already ends. So the thing to assert is that a
    spent Pace leaves no pass able to start work -- everything else follows from
    the loop that was already there.
    """

    def a_manager(self, tmp_path, patience=8):
        from cvise.utils import statistics, testing

        case = tmp_path / 'a.c'
        case.write_text('int a(void) { return 1; }\n')
        script = tmp_path / 'check.sh'
        script.write_text('#!/bin/sh\nexit 0\n')
        script.chmod(0o744)
        return testing.TestManager(
            statistics.PassStatistic(), script, 100, False, [case], 1, False, True,
            False, False, False, None, False, None, None, None, 1.0,
            patience=patience,
        )

    def a_context(self):
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ctx.defunct = False
        return ctx

    def replay_into(self, m, run, silent_for=None):
        """Give the manager this run's history, laid out on the real clock.

        Both details are load-bearing. Replacing the whole Pace would replace
        the patience with the default, and the test for --patience 0 would then
        assert nothing. And the manager asks `spent()` with no argument, so it
        compares against time.monotonic(): a history written from zero is
        however many days of uptime old, and every one of these tests would
        pass because the machine had been up a while.
        """
        silent_for = run['tail'] if silent_for is None else silent_for
        at = time.monotonic() - sum(run['gaps']) - silent_for
        for gap, lines in zip([0] + run['gaps'], run['lines']):
            at += gap
            m.pace.record(lines, now=at)

    def test_a_spent_run_leaves_no_pass_able_to_work(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        m = self.a_manager(tmp_path)
        m.pass_contexts = [self.a_context() for _ in range(3)]
        self.replay_into(m, DEAD)

        assert m.call_it_finished_if_it_is()
        assert all(c.defunct for c in m.pass_contexts)
        assert 'nothing smaller found for' in caplog.text

    def test_a_run_still_finding_things_is_left_alone(self, tmp_path):
        m = self.a_manager(tmp_path)
        m.pass_contexts = [self.a_context() for _ in range(3)]
        self.replay_into(m, HEALTHY_A)

        assert not m.call_it_finished_if_it_is()
        assert not any(c.defunct for c in m.pass_contexts)

    def test_it_says_so_once_and_not_every_time_round_the_loop(self, tmp_path, caplog):
        """It is asked once per iteration of a loop that spins on every job."""
        m = self.a_manager(tmp_path)
        caplog.set_level(logging.INFO)
        m.pass_contexts = [self.a_context()]
        self.replay_into(m, DEAD)

        assert m.call_it_finished_if_it_is()
        for _ in range(50):
            assert not m.call_it_finished_if_it_is()
        assert caplog.text.count('nothing smaller found for') == 1

    def test_patience_of_zero_waits_for_ever(self, tmp_path):
        """What the CLI turns --patience 0 into, and what C-Vise used to do."""
        m = self.a_manager(tmp_path, patience=10 ** 9)
        m.pass_contexts = [self.a_context()]
        self.replay_into(m, DEAD)

        assert not m.call_it_finished_if_it_is()


class TestBothImplementationsAgree:
    """One fixture, two readers, in two languages.

    The decision to stop is C-Vise's and cvise-mon only displays -- but it
    displays the same quantities computed a second time in Rust, and two copies
    of a rule drift. The expectations were computed from this module and are
    checked from the Rust side too, so a change on either side fails a test.
    """

    DATA = Path(__file__).parent / 'data'

    def read(self, path):
        p = Pace(started=0.0)
        for line in path.read_text().splitlines():
            if line.startswith('#run'):
                p = Pace(started=0.0)
                continue
            if line.startswith('#'):
                continue
            when, _bytes, lines, *_rest = line.split('\t')
            p.record(int(lines), now=float(when))
        return p

    def test_the_expectations_still_describe_this_code(self):
        p = self.read(self.DATA / 'run16-progress.tsv')
        rows = [line.split('\t') for line in
                (self.DATA / 'run16-expected.tsv').read_text().splitlines()
                if not line.startswith('#')]
        assert len(rows) >= 8, 'the fixture stopped covering both sides of the deadline'
        for now, usual, deadline, spent, line in rows:
            assert abs(p.usual - float(usual)) < 0.001
            assert abs(p.deadline() - float(deadline)) < 1.0
            assert p.spent(now=float(now)) is (spent == 'true'), now
            assert report(p, now=float(now)) == line, now

    def test_the_fixture_straddles_the_moment_it_is_about(self):
        """An expectation file that is all one answer proves nothing."""
        verdicts = {line.split('\t')[3] for line in
                    (self.DATA / 'run16-expected.tsv').read_text().splitlines()
                    if not line.startswith('#')}
        assert verdicts == {'true', 'false'}
