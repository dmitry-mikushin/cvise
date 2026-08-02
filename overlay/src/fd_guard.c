/*
    libcvise_overlay -- the reduction overlay

    This library is free software; you can redistribute it and/or
    modify it under the terms of the GNU Lesser General Public
    License as published by the Free Software Foundation; either
    version 2.1 of the License, or (at your option) any later version.
*/


#include <config.h>

#include <errno.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include "libfakechroot.h"


/* Mutations that arrive through a file descriptor.
 *
 * This overlay works on paths: it rewrites a name before the kernel sees it. A
 * descriptor has no name -- by the time a job holds one, the decision about
 * which file it refers to has already been made. If that decision was "the
 * shared original", because the job had not written the file and so read it
 * from the pristine tree, then ftruncate, fchmod, fchown or futimens on that
 * descriptor reach the inode every other job is reading. Nothing about the call
 * says which file it is, so nothing can redirect it.
 *
 * What can be said with certainty is which descriptors are safe: the ones this
 * job opened for writing, which the path layer already sent into the delta. A
 * descriptor opened read-only cannot be pointing at a file the job owns, so a
 * mutation through it is a mutation of the shared tree, and refusing it is both
 * correct and the only honest option -- the alternative is to let one job
 * silently corrupt what every other job is compiling.
 */

static int refuse_shared_fd (int fd)
{
    int flags = fcntl(fd, F_GETFL);

    if (flags < 0)
        return 0;
    if ((flags & O_ACCMODE) == O_RDONLY) {
        /* Read-only: whatever this is, the job does not own it. */
        errno = EROFS;
        return 1;
    }
    return 0;
}


wrapper(ftruncate, int, (int fd, off_t length))
{
    debug("ftruncate(%d, %ld)", fd, (long) length);
    if (refuse_shared_fd(fd))
        return -1;
    return nextcall(ftruncate)(fd, length);
}


wrapper(fchmod, int, (int fd, mode_t mode))
{
    debug("fchmod(%d, %o)", fd, (unsigned) mode);
    if (refuse_shared_fd(fd))
        return -1;
    return nextcall(fchmod)(fd, mode);
}


wrapper(fchown, int, (int fd, uid_t owner, gid_t group))
{
    debug("fchown(%d, %d, %d)", fd, (int) owner, (int) group);
    if (refuse_shared_fd(fd))
        return -1;
    return nextcall(fchown)(fd, owner, group);
}


#ifdef HAVE_FUTIMENS
wrapper(futimens, int, (int fd, const struct timespec times[2]))
{
    debug("futimens(%d, ...)", fd);
    if (refuse_shared_fd(fd))
        return -1;
    return nextcall(futimens)(fd, times);
}
#endif
