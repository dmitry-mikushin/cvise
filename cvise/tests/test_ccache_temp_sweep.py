"""The temporary files ccache leaks, and why nothing else collects them.

MEASURED on a seven-hour reduction: the cache held 9.8 GB against a 20 GB
ceiling while `ccache/tmp` held 41 GB in 14 175 files -- two thirds of the tmpfs
the run lives in. ccache sweeps those only during a cleanup, and a cleanup runs
only when the cache exceeds its maximum size, which a cache well under its cap
never does. The loop is closed and nothing ever breaks it.

The tests here are about the two ways the sweep could fail while looking like it
worked: pointed at the wrong directory it deletes nothing and says nothing, and
without an age it races a running compiler.
"""

import importlib.util
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[2] / 'cvise-ns-projection.py'


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location('ns_projection_runner', RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def commands(runner, monkeypatch):
    seen = []

    def record(command, **kwargs):
        seen.append(command)

        class Result:
            returncode = 0
            stdout = ''
            stderr = ''

        return Result()

    monkeypatch.setattr(runner.subprocess, 'run', record)
    return seen


def test_it_sweeps_the_temporary_directory_and_not_the_cache(runner, commands, tmp_path):
    """The cache is the point of the cache. Only tmp/ is collected here.

    Asserted on what `find` is pointed at, not on whether the cache path appears
    at all: it legitimately appears in the bind mount, and a test that forbade
    that would pass only while the command was written a particular way.
    """
    ccache = tmp_path / 'ccache'
    (ccache / 'tmp').mkdir(parents=True)
    runner.sweep_ccache_temp(ccache)

    assert len(commands) == 1
    command = commands[0]
    target = command[command.index('find') + 1]
    assert target == f'{ccache}/tmp'
    assert '-delete' in command
    assert '-type' in command and command[command.index('-type') + 1] == 'f'


def test_it_refuses_to_delete_anything_recent(runner, commands, tmp_path):
    """The age is the whole safety argument.

    A compile takes seconds, so an hour is far past any live one -- but without
    the bound this deletes the file a running compiler is writing.
    """
    ccache = tmp_path / 'ccache'
    (ccache / 'tmp').mkdir(parents=True)
    runner.sweep_ccache_temp(ccache)

    command = commands[0]
    assert '-mmin' in command
    minutes = command[command.index('-mmin') + 1]
    assert minutes.startswith('+'), 'without + this matches files YOUNGER than the age'
    assert int(minutes[1:]) >= 60


def test_it_goes_through_a_container(runner, commands, tmp_path):
    """The files were written by a container, as root.

    A host-side unlink gets EPERM on every one of them, and an ignored EPERM is
    a sweep that reports success and frees nothing.
    """
    ccache = tmp_path / 'ccache'
    (ccache / 'tmp').mkdir(parents=True)
    runner.sweep_ccache_temp(ccache)

    command = commands[0]
    assert command[:3] == ['docker', 'run', '--rm']
    assert runner.IMAGE in command


def test_an_absent_cache_is_not_an_error(runner, commands, tmp_path):
    runner.sweep_ccache_temp(tmp_path / 'never-created')
    assert commands == []
