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
import tempfile
from pathlib import Path


CGROUP_ROOT = Path('/sys/fs/cgroup')

# Returned when the cgroup hierarchy cannot be inspected from here -- typically
# inside a cgroup namespace, where the workload may well be bounded by a limit
# this process is not allowed to see.
UNKNOWN_CEILING = -1

# How much of what the machine has free a reduction may claim. The rest is for
# everything else on the machine, including the page cache the compilers need.
CEILING_FRACTION = 0.75


def sweep_stale_cgroups(parent: Path, prefix: str = 'cvise-') -> int:
    """Remove the empty cgroups earlier runs could not remove themselves.

    A process cannot rmdir the cgroup it lives in, and once it has exited there
    is nobody left to try, so each run leaves one behind. Removing one that is
    not empty is impossible rather than merely discouraged -- the kernel refuses
    -- so this cannot disturb a run that is still going.
    """
    removed = 0
    try:
        candidates = sorted(parent.glob(prefix + '*'))
    except OSError:
        return 0
    for node in candidates:
        if not node.is_dir():
            continue
        try:
            node.rmdir()
            removed += 1
        except OSError:
            pass  # still in use, or not ours to remove
    if removed:
        logging.debug('removed %d cgroups left by earlier runs', removed)
    return removed


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

    # Every run leaves its cgroup behind: a process cannot remove the cgroup it
    # is sitting in, and by the time it has left there is nobody to do it. They
    # are empty and cost almost nothing each, but MEASURED after a day of work:
    # 424 of them. Sweeping the empty ones here is safe by construction -- rmdir
    # on a cgroup with anything in it fails -- and it means the litter cannot
    # outlive the next run rather than accumulating forever.
    sweep_stale_cgroups(parent)

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


def filesystem_of(path) -> str:
    """The kind of filesystem a path is on, according to the kernel.

    Read from /proc/self/mountinfo rather than guessed from the name: /tmp is a
    tmpfs on most modern systems and a disk on plenty of others, and a warning
    that assumes either is wrong for half its audience.
    """
    try:
        target = Path(path).resolve()
    except OSError:
        return ''
    best = ''
    kind = ''
    try:
        with open('/proc/self/mountinfo') as f:
            for line in f:
                fields = line.split()
                try:
                    separator = fields.index('-')
                except ValueError:
                    continue
                mount_point = fields[4]
                fs_type = fields[separator + 1]
                if (target == Path(mount_point) or Path(mount_point) in target.parents) and len(
                    mount_point
                ) >= len(best):
                    best = mount_point
                    kind = fs_type
    except OSError:
        return ''
    return kind


RAM_FILESYSTEMS = ('tmpfs', 'ramfs')


def scratch_is_ram(path) -> bool:
    """Is the scratch space this run will use held in memory?

    This is the question the ceiling exists for. A reduction whose scratch is on
    a disk cannot take the machine down by filling it, however large it grows;
    one whose scratch is a tmpfs can, and will, because those pages are charged
    to nobody the OOM killer can kill.
    """
    return filesystem_of(path) in RAM_FILESYSTEMS


def bound_or_warn(scratch=None) -> int | None:
    """Acquire a ceiling if there is none, and say so plainly if it cannot be.

    The warning is about a combination, not about a missing feature. Scratch in
    memory with no ceiling is the arrangement that takes a machine down; either
    one alone is fine, and warning about either alone teaches the user to ignore
    the warning by the time the dangerous one arrives.
    """
    existing = memory_ceiling()
    if existing == UNKNOWN_CEILING:
        return None
    ram = total_ram()
    if existing is not None:
        if not ram or existing < ram:
            return existing
        # A limit at or above physical memory is a number, not a ceiling: the
        # machine dies of its own scratch space long before the cgroup notices.
        logging.info(
            'the memory limit on this cgroup is %.1f GiB, at or above the %.1f GiB this machine '
            'has, so it bounds nothing; looking for a real one',
            existing / 2**30,
            ram / 2**30,
        )

    budget = int(available_ram() * CEILING_FRACTION)
    if budget > 0:
        acquired = bound_this_process(budget)
        if acquired:
            logging.info('this reduction is bounded to %.1f GiB of memory', acquired / 2**30)
            return acquired

    scratch = tempfile.gettempdir() if scratch is None else scratch
    if not scratch_is_ram(scratch):
        logging.info(
            'no memory ceiling could be acquired, but the scratch space in %s is on %s rather '
            'than in memory, so a reduction that outgrows it will fail rather than take the '
            'machine with it',
            scratch,
            filesystem_of(scratch) or 'a filesystem of unknown kind',
        )
        return None

    logging.warning(
        'no memory ceiling could be acquired and the scratch space in %s is held in memory '
        '(%s). Every job materialises the part of the build its candidate changed there, '
        'including the linked binary, and those pages are not reclaimable and cannot be freed '
        'by the OOM killer -- so a reduction that outgrows RAM will take the machine down '
        'rather than fail. Either point TMPDIR at a disk, or run inside something that bounds '
        'memory: a container with --memory, or systemd-run --user --scope -p MemoryMax=...',
        scratch,
        filesystem_of(scratch),
    )
    return None

    scratch = tempfile.gettempdir() if scratch is None else scratch
    if not scratch_is_ram(scratch):
        logging.info(
            'no memory ceiling could be acquired, but the scratch space in %s is on %s rather '
            'than in memory, so a reduction that outgrows it will fail rather than take the '
            'machine with it',
            scratch,
            filesystem_of(scratch) or 'a filesystem of unknown kind',
        )
        return None

    logging.warning(
        'no memory ceiling could be acquired and the scratch space in %s is held in memory '
        '(%s). Every job materialises the part of the build its candidate changed there, '
        'including the linked binary, and those pages are not reclaimable and cannot be freed '
        'by the OOM killer -- so a reduction that outgrows RAM will take the machine down '
        'rather than fail. Either point TMPDIR at a disk, or run inside something that bounds '
        'memory: a container with --memory, or systemd-run --user --scope -p MemoryMax=...',
        scratch,
        filesystem_of(scratch),
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


