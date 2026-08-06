"""Killing a candidate that spawns, and the escape route it used.

MEASURED, and it ended a reduction after eleven hours. A candidate's test
binary re-executed itself, appending another --gtest_list_tests to its own argv
each time. The candidate was killed. 43 332 of its children were not: killing
the parent reparents every survivor to PID 1, and from that moment they are not
descendants of the pid the killer is walking from, so the walk never sees them
again. In the container PID 1 is C-Vise itself, which does not kill what it did
not start, so they stayed -- holding 134 GiB of anonymous memory -- until the
run could no longer execute its own test and stopped on its undecided budget.

A process group id does not change when a parent dies, and a group started with
start_new_session has an id equal to the pid we already hold. That is the fix,
and it is addressed by number rather than looked up through the leader: in this
failure the leader was the first thing to die.
"""

import os
import signal
import subprocess
import time

import psutil
import pytest

from cvise.utils.process import signal_own_group


def alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_an_orphan_whose_parent_is_gone_is_still_killed():
    """The exact shape of the failure, in miniature.

    The child spawns a grandchild and exits at once, so the grandchild is
    adopted before anything can walk the tree, and the pid the killer was given
    no longer has any descendants at all. Only the group still connects them --
    and only if the group is addressed by number, since the leader is gone.
    """
    child = subprocess.Popen(
        ['sh', '-c', 'sleep 120 & echo $! ; exit 0'],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    grandchild = int(child.stdout.readline().strip())
    leader = child.pid
    child.wait()

    assert alive(grandchild), 'the grandchild should have outlived its parent'
    assert psutil.Process(grandchild).ppid() != leader, 'it should have been adopted'
    assert os.getpgid(grandchild) == leader, 'but it should still be in the leader group'

    signal_own_group(leader, signal.SIGKILL)

    for _ in range(50):
        if not alive(grandchild):
            break
        time.sleep(0.1)
    assert not alive(grandchild), 'the orphan survived the group kill'


def test_walking_the_tree_would_not_have_found_it():
    """Why the walk is not enough, stated as a test rather than as a comment."""
    child = subprocess.Popen(
        ['sh', '-c', 'sleep 120 & echo $! ; exit 0'],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    grandchild = int(child.stdout.readline().strip())
    leader = child.pid
    child.wait()
    try:
        with pytest.raises(psutil.NoSuchProcess):
            psutil.Process(leader).children(recursive=True)
    finally:
        signal_own_group(leader, signal.SIGKILL)


def test_it_refuses_to_signal_the_group_we_are_in(monkeypatch):
    """The guard that keeps this from killing C-Vise itself.

    Recorded rather than executed: a test that gets this wrong should fail, not
    take the test runner down with it.
    """
    sent = []
    monkeypatch.setattr(os, 'killpg', lambda pgid, sig: sent.append((pgid, sig)))

    signal_own_group(os.getpgrp(), signal.SIGKILL)

    assert sent == [], 'signalled the group C-Vise itself is in'


def test_a_group_that_no_longer_exists_is_not_an_error():
    child = subprocess.Popen(['true'], start_new_session=True)
    child.wait()
    signal_own_group(child.pid, signal.SIGKILL)


def test_run_process_gives_the_candidate_a_group_of_its_own():
    """What the notifier does, asked of the kernel rather than of the source.

    A process in a session of its own has a process group whose id equals its
    pid; without start_new_session it inherits ours.
    """
    from cvise.utils import sigmonitor
    from cvise.utils.process import ProcessEventNotifier

    sigmonitor.init()
    notifier = ProcessEventNotifier(pid_queue=None)
    out, _, _ = notifier.run_process(['sh', '-c', 'echo $$; ps -o pgid= -p $$'])
    pid, pgid = (int(x) for x in out.decode().split())
    assert pid == pgid, 'the candidate did not lead a group of its own'
