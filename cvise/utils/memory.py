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

import os
import resource
from pathlib import Path

from cvise.utils.error import CViseError

CGROUP_ROOT = Path('/sys/fs/cgroup')


class NoMemoryCeilingError(CViseError):
    def __init__(self, cgroup):
        self.cgroup = cgroup

    def __str__(self):
        return (
            f'This reduction is running in cgroup {self.cgroup}, which has no memory ceiling. '
            'Refusing to start: the scratch space is a tmpfs, tmpfs pages are not reclaimable, '
            'and the OOM killer cannot free them -- so a reduction that outgrows RAM takes the '
            'machine with it instead of failing. Start it under a ceiling, for example:\n'
            '    systemd-run --user --scope -p MemoryMax=64G -p MemorySwapMax=0 cvise ...\n'
            'The ceiling bounds the scratch space and the compilers together, because tmpfs '
            'pages are charged to the cgroup that writes them. It is not a limit on how many '
            'jobs run at once, and lowering the job count is not an alternative to it.'
        )


def current_cgroup() -> str:
    try:
        with open('/proc/self/cgroup') as f:
            for line in f:
                parts = line.strip().split(':', 2)
                if len(parts) == 3 and parts[0] == '0':
                    return parts[2]
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


def guard_memory_ceiling(required: bool = True) -> int | None:
    """Refuse to run without a ceiling, when one is demanded."""
    ceiling = memory_ceiling()
    if ceiling is None and required:
        raise NoMemoryCeilingError(current_cgroup() or '<unknown>')
    return ceiling


def ceiling_required() -> bool:
    return os.environ.get('CVISE_REQUIRE_MEMORY_CEILING', '') not in ('', '0')


def apply_test_memory_limit(limit_bytes: int) -> None:
    """Bound one interestingness test, to be called in the child before exec.

    A second line only: where cgroups are available they bound everything the
    test spawns, including its tmpfs writes, which an rlimit cannot do.
    """
    if limit_bytes <= 0:
        return
    resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
