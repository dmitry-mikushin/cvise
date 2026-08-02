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


def bound_this_process(limit_bytes: int) -> int | None:
    """Put THIS process under a memory limit, without spawning anything.

    The limit is not a user-facing decision -- it is what keeps a reduction from
    taking the machine down, and there is one right answer: most of what the
    machine has free, and no swap, since swapping at this scale is a slower
    death rather than a lesser one. So C-Vise arranges it rather than asking the
    user to write systemd-run in front of every invocation.

    It does so in place. Re-executing under a transient scope also works, but
    everything the user can see and signal is then the wrapper rather than the
    reduction: Control-C has to be forwarded by hand, a child killed by a signal
    reports an exit status that has to be translated back, there is a window
    between spawning and installing the handlers, and the marker that prevents
    recursion is inherited by every child -- so an interestingness test that
    itself runs C-Vise would run it unprotected. None of that exists if no
    second process does.

    The delegation that systemd-run relies on is the same permission as writing
    cgroup.procs directly, so nothing is gained by the detour.
    """
    cgroup = current_cgroup()
    if not cgroup or cgroup == '/':
        return None
    node = CGROUP_ROOT / cgroup.lstrip('/')
    parent = node.parent
    if not parent.is_dir():
        return None

    # The memory controller has to be delegated to the parent before a child of
    # it can have a memory.max at all.
    try:
        controllers = (parent / 'cgroup.controllers').read_text().split()
    except OSError:
        return None
    if 'memory' not in controllers:
        return None
    try:
        if 'memory' not in (parent / 'cgroup.subtree_control').read_text().split():
            (parent / 'cgroup.subtree_control').write_text('+memory')
    except OSError:
        return None

    own = parent / f'cvise-{os.getpid()}'
    try:
        own.mkdir(exist_ok=True)
        (own / 'memory.max').write_text(str(limit_bytes))
        try:
            (own / 'memory.swap.max').write_text('0')
        except OSError:
            # Not fatal: a machine without swap has nothing to bound here.
            pass
        (own / 'cgroup.procs').write_text(str(os.getpid()))
    except OSError as e:
        logging.debug('cannot bound this process in %s: %s', own, e)
        return None
    return limit_bytes


def bound_or_warn() -> int | None:
    """Acquire a ceiling if there is none, and say so plainly if it cannot be."""
    existing = memory_ceiling()
    if existing is not None and existing != UNKNOWN_CEILING:
        return existing
    if existing == UNKNOWN_CEILING:
        return None

    budget = int(available_ram() * CEILING_FRACTION)
    if budget > 0:
        acquired = bound_this_process(budget)
        if acquired:
            logging.info('this reduction is bounded to %.1f GiB of memory', acquired / 2**30)
            return acquired

    logging.warning(
        'no memory ceiling could be acquired: a reduction that outgrows RAM will take the '
        'machine down rather than fail, because tmpfs pages are not reclaimable and the OOM '
        'killer cannot free them. Run inside something that bounds memory -- a container with '
        '--memory, or systemd-run --user --scope -p MemoryMax=...'
    )
    return None


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


