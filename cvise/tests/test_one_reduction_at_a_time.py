"""One reduction at a time, and what "at a time" has to mean.

A lock that only catches a live competitor does not catch the case it is
actually wanted for. A runner can die while its container keeps running: the
reduction then holds 88 cores and 150 GiB of the machine, and no lock is held
for it, because the lock died with the process that took it. Every test here
corresponds to one of the two ways the machine can already be busy.
"""

import fcntl
import importlib.util
import os
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[2] / 'cvise-ns-projection.py'


@pytest.fixture
def runner(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('ns_projection_runner', RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # A lock of its own, so a test never contends with a reduction that may be
    # running on this machine -- and never takes the real one away from it.
    monkeypatch.setattr(module, 'LOCK', tmp_path / 'cvise.lock')
    monkeypatch.setattr(module, 'reduction_containers', lambda: [])
    return module


def test_the_first_run_takes_it(runner):
    handle = runner.take_the_machine('first')
    assert (runner.LOCK).read_text().startswith(f'pid {os.getpid()}')
    os.close(handle)


def test_a_second_run_is_refused_and_says_who_holds_it(runner):
    held = os.open(runner.LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(held, fcntl.LOCK_EX)
    os.write(held, b'pid 4242  run other  started 2026-08-06 14:00:00\n')

    with pytest.raises(SystemExit) as refusal:
        runner.take_the_machine('second')
    assert refusal.value.code == 1
    os.close(held)


def test_the_lock_is_released_when_the_holder_goes(runner):
    """flock and not a pid file, so a killed runner does not leave a lock.

    A lock that outlives its holder has to be broken by hand, and a lock that
    is routinely broken by hand is not a lock.
    """
    first = runner.take_the_machine('first')
    os.close(first)
    second = runner.take_the_machine('second')
    os.close(second)


def test_an_abandoned_container_is_refused_although_nobody_holds_the_lock(runner, monkeypatch):
    """The case the lock exists for.

    Nothing holds the lock -- so flock says the machine is free -- while a
    reduction container from a dead runner still has the whole machine.
    """
    monkeypatch.setattr(runner, 'reduction_containers',
                        lambda: [('2a5801b6e21e', 'Up 3 hours')])
    with pytest.raises(SystemExit) as refusal:
        runner.take_the_machine('second')
    assert refusal.value.code == 1


def test_it_does_not_stop_the_abandoned_container_itself(runner, monkeypatch):
    """Naming it, not killing it: it may be hours of somebody's work, and the
    judgement that it is abandoned is an inference made in a startup path."""
    stopped = []
    monkeypatch.setattr(runner, 'reduction_containers',
                        lambda: [('2a5801b6e21e', 'Up 3 hours')])
    monkeypatch.setattr(runner.subprocess, 'run',
                        lambda *a, **k: stopped.append(a) or (_ for _ in ()).throw(
                            AssertionError('the lock ran a command')))
    with pytest.raises(SystemExit):
        runner.take_the_machine('second')
    assert stopped == []
