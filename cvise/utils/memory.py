"""Keep a reduction from taking the machine down with it.

A reduction is a long-running, highly parallel workload that writes a lot and
allocates a lot, and there is a specific way it kills a machine. Its scratch
space lives on a tmpfs -- /tmp or /dev/shm -- because that is roughly a hundred
times faster than a disk, and that is the whole point. But tmpfs pages are not
page cache: they are not reclaimable, and the OOM killer cannot free them,
because it only kills processes. Once the scratch space owns most of RAM,
everything on the machine starts dying and nothing improves, because the memory
is not in any process.

MEASURED in a 2 GiB VM: a scratch tmpfs holding 980 MiB, then six 256 MiB
children -- three were OOM-killed, and Shmem afterwards was still 980 MiB. The
kernel freed nothing that mattered.

The fix is NOT to give up the fast scratch, and it is NOT to run fewer jobs.
Both would pay for safety with the throughput the machine was bought for. It is
to put a ceiling under the whole workload, because tmpfs pages are charged to
the cgroup of the process that writes them -- MEASURED, same VM: a cgroup with
memory.max=400 MiB, writing into a tmpfs mounted with NO size= at all, was
stopped exactly at 400 MiB of Shmem by its own cgroup, and the machine was
never at risk. One ceiling therefore bounds the scratch and the compilers at
once, whatever the mix between them happens to be at any moment.

So C-Vise checks that it is running under such a ceiling, and says how to get
one if it is not. It does not lower the ceiling for anyone, and it does not
care how many jobs run underneath it.
"""

import logging
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from cvise.utils.error import CViseError

CGROUP_ROOT = Path('/sys/fs/cgroup')

# Returned when the cgroup hierarchy cannot be inspected from here -- typically
# inside a cgroup namespace, where the workload may well be bounded by a limit
# this process is not allowed to see.
UNKNOWN_CEILING = -1

# How much of what the machine has free a reduction may claim. The rest is for
# everything else on the machine, including the page cache the compilers need.
CEILING_FRACTION = 0.75


class NoMemoryCeilingError(CViseError):
    def __init__(self, cgroup):
        self.cgroup = cgroup

    def __str__(self):
        return (
            f'This reduction is running in cgroup {self.cgroup}, which has no memory ceiling, '
            'and C-Vise could not give itself one. Refusing to start: the scratch space is a '
            'tmpfs, tmpfs pages are not reclaimable, and the OOM killer cannot free them -- so '
            'a reduction that outgrows RAM takes the machine with it instead of failing. '
            'Normally C-Vise places itself under a limit automatically; that needs a systemd '
            'user session with the memory controller delegated. Without one, run it inside '
            'anything that bounds memory -- a container with --memory, or a cgroup of your own.'
        )


RELAUNCH_MARKER = 'CVISE_UNDER_MEMORY_CEILING'


def relaunch_under_ceiling(argv: list[str]) -> bool:
    """Put this process under a memory limit, rather than asking the user to.

    The limit is not a user-facing decision: it is what keeps a reduction from
    taking the machine down, and there is one right answer -- most of what the
    machine has free, and no swap, since swapping at this scale is just a slower
    death. Making the user write `systemd-run --user --scope -p MemoryMax=...`
    in front of every invocation exposes an implementation detail and gets
    forgotten exactly once.

    Returns True when it has re-executed C-Vise inside a scope, in which case
    the caller should stop.
    """
    if os.environ.get(RELAUNCH_MARKER):
        return False  # this IS the relaunched process
    if memory_ceiling() is not None:
        return False  # already bounded by whoever started us
    if shutil.which('systemd-run') is None:
        return False

    budget = int(available_ram() * CEILING_FRACTION)
    if budget <= 0:
        return False

    env = dict(os.environ, **{RELAUNCH_MARKER: '1'})
    command = [
        'systemd-run', '--user', '--scope', '--quiet',
        '-p', f'MemoryMax={budget}',
        '-p', 'MemorySwapMax=0',
        '--',
    ] + argv
    logging.info('placing this reduction under a %.1f GiB memory ceiling', budget / 2**30)
    try:
        child = subprocess.Popen(command, env=env)
    except OSError:
        return False

    # The reduction now lives inside the scope, so the process the user can see
    # and signal is this one. Without forwarding, Control-C reaches the wrapper
    # and the reduction carries on -- the ceiling would have cost the user the
    # ability to stop their own run.
    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)

    sys.exit(child.wait())


def current_cgroup() -> str:
    """The v2 path for this process, or '/' when it is in the root cgroup.

    An empty string means "not on cgroup v2 at all", which is a different thing
    from "in the root cgroup" and must not be confused with it: the root cgroup
    has a readable memory.max like any other.
    """
    try:
        with open('/proc/self/cgroup') as f:
            for line in f:
                parts = line.strip().split(':', 2)
                if len(parts) == 3 and parts[0] == '0':
                    return parts[2] or '/'
    except OSError:
        pass
    return ''


def memory_ceiling() -> int | None:
    """The nearest finite memory.max above this process, in bytes.

    A limit set on any ancestor bounds us just as well as one set on our own
    group, so the whole chain is walked rather than only the leaf.
    """
    rel = current_cgroup()
    if not rel:
        return None
    node = CGROUP_ROOT / rel.lstrip('/')
    if not node.exists():
        # A cgroup namespace reports a path that does not exist in the
        # /sys/fs/cgroup this process can see. That is not "no ceiling", it is
        # "cannot tell", and the two must not share a return value: a bounded
        # container reports exactly this, and refusing there would be the
        # opposite of the intent.
        return UNKNOWN_CEILING
    while True:
        try:
            value = (node / 'memory.max').read_text().strip()
        except OSError:
            value = ''
        if value and value != 'max':
            return int(value)
        if node == CGROUP_ROOT or CGROUP_ROOT not in node.parents:
            return None
        node = node.parent


def available_ram() -> int:
    """What the machine can actually spare right now, not what it has in total."""
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def total_ram() -> int:
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def guard_memory_ceiling(required: bool) -> int | None:
    """Check for a ceiling; refuse only when this run demands one.

    Demanding one unconditionally would make an ordinary reduction of a single
    file refuse to start on a normal desktop, which trades one failure mode for
    a worse one. The ceiling matters for the workload that actually threatens
    the machine -- a whole project, many jobs, a RAM-backed scratch -- so that
    is where it is required, and everywhere else it is said out loud and left
    to the operator.
    """
    ceiling = memory_ceiling()
    if ceiling == UNKNOWN_CEILING:
        logging.info('cannot inspect the cgroup hierarchy from here; assuming the '
                     'workload is bounded by a limit set outside this namespace')
        return None
    if ceiling is None:
        if not required:
            logging.warning(
                'no memory ceiling on this cgroup: a reduction that outgrows RAM will take '
                'the machine down rather than fail, because tmpfs pages are not reclaimable '
                'and the OOM killer cannot free them. Consider: '
                'systemd-run --user --scope -p MemoryMax=... -p MemorySwapMax=0 cvise ...'
            )
            return None
        raise NoMemoryCeilingError(current_cgroup() or '<unknown>')
    ram = total_ram()
    if ceiling is not None and ram and ceiling >= ram:
        # A limit at or above physical memory is a number, not a ceiling: the
        # machine dies of its own scratch space long before the cgroup notices.
        raise NoMemoryCeilingError(
            f'{current_cgroup() or "<unknown>"} (memory.max is {ceiling >> 30} GiB, '
            f'at or above the {ram >> 30} GiB this machine has)'
        )
    return ceiling


