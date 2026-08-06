"""Refusing a candidate that spawns without bound.

MEASURED, and it cost eleven hours. A candidate's test binary re-executed
itself; 43 332 of its processes were left holding 134 GiB, and the reduction
kept going for an hour before it could no longer run its test at all and
stopped on its undecided-verdict budget.

Correctness was never at stake: the test could not decide, and an undecided
candidate keeps its previous state, so the swarm was never published --
measured afterwards on the published tree, which contains no such thing.
Availability was at stake, and the run died of it.

Killing the swarm from outside cures the symptom. These tests are about the
verdict: the candidate that produced it is refused, here, where the candidate
is, and while refusing it is still cheap.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path


CHECK = 'cvise.utils.projectcheck'


def in_its_own_session(body: str, tmp_path) -> subprocess.CompletedProcess:
    """Run the body in a session of its own.

    Not in the test runner's: the watchdog kills every process in its group,
    and in pytest's group that is pytest.
    """
    script = tmp_path / 'swarm.py'
    script.write_text(textwrap.dedent(body))
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env['PYTHONPATH'] = str(root) + os.pathsep + env.get('PYTHONPATH', '')
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        start_new_session=True,
        env=env,
    )


def test_a_swarm_is_noticed_and_dispersed(tmp_path):
    result = in_its_own_session(
        f'''
        import os, subprocess, threading, time
        from {CHECK} import watch_for_a_swarm, my_process_group

        children = [subprocess.Popen(['sleep', '60']) for _ in range(40)]
        done = threading.Event()
        seen = watch_for_a_swarm(ceiling=20, done=done)
        time.sleep(1)
        alive = sum(1 for c in children if c.poll() is None)
        print('seen', len(seen))
        print('alive_after', alive)
        for c in children:
            c.kill()
        ''',
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    seen = int(result.stdout.split('seen ')[1].split()[0])
    alive = int(result.stdout.split('alive_after ')[1].split()[0])
    assert seen > 20, f'the swarm was not noticed: {result.stdout}'
    assert alive == 0, f'{alive} of the swarm survived being dispersed'


def test_a_quiet_candidate_is_not_touched(tmp_path):
    """The other half: this must not fire on an ordinary candidate.

    A check that refuses everything is not a check, and the ceiling sits far
    above the handful of processes an exact-filter ctest run needs.
    """
    result = in_its_own_session(
        f'''
        import subprocess, threading, time
        from {CHECK} import watch_for_a_swarm

        children = [subprocess.Popen(['sleep', '4']) for _ in range(5)]
        done = threading.Event()
        watcher_result = []

        def watch():
            watcher_result.append(watch_for_a_swarm(ceiling=256, done=done))

        t = threading.Thread(target=watch)
        t.start()
        time.sleep(3)
        done.set()
        t.join()
        print('seen', len(watcher_result[0]))
        print('alive', sum(1 for c in children if c.poll() is None))
        for c in children:
            c.kill()
        ''',
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert 'seen 0' in result.stdout, f'fired on a quiet candidate: {result.stdout}'
    assert 'alive 5' in result.stdout, f'killed a quiet candidate: {result.stdout}'


def test_the_group_is_the_candidate_and_not_the_machine(tmp_path):
    """What is counted, asked of the kernel.

    The group contains what this candidate started and nothing else, however
    busy the machine is -- which is why it can be counted exactly instead of
    guessed at from process names.
    """
    result = in_its_own_session(
        f'''
        import os, subprocess
        from {CHECK} import my_process_group

        before = len(my_process_group())
        kids = [subprocess.Popen(['sleep', '30']) for _ in range(7)]
        after = len(my_process_group())
        print('delta', after - before)
        print('leader_is_us', os.getpgrp() == os.getpid())
        for k in kids:
            k.kill()
        ''',
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert 'delta 7' in result.stdout, result.stdout
    assert 'leader_is_us True' in result.stdout, result.stdout
