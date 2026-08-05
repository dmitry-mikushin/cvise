/*
    libfakechroot -- fake chroot environment
    Copyright (c) 2010, 2013 Piotr Roszatycki <dexter@debian.org>

    This library is free software; you can redistribute it and/or
    modify it under the terms of the GNU Lesser General Public
    License as published by the Free Software Foundation; either
    version 2.1 of the License, or (at your option) any later version.

    This library is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
    Lesser General Public License for more details.

    You should have received a copy of the GNU Lesser General Public
    License along with this library; if not, write to the Free Software
    Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307  USA
*/


#include <config.h>

#ifdef HAVE_POSIX_SPAWN_FILE_ACTIONS_ADDOPEN

#include <spawn.h>
#include <fcntl.h>

#include "libfakechroot.h"


/* The path is redirected here, where it is recorded, and not where it is used.
 *
 * A file action is carried out by the spawn implementation itself, in the child
 * between fork and exec, before the new program image exists. glibc does that
 * open with an internal call that never reaches this library, so a path handed
 * to it arrives at the kernel untouched however thoroughly open() is wrapped.
 *
 * MEASURED, with the delta and the shared tree both in place: a child spawned
 * with an action redirecting its output to a path under the reduction root
 * created that file in the SHARED TREE, which every other job in the run is
 * reading. Nothing about it is visible afterwards -- the job succeeds, and what
 * it damaged belongs to somebody else.
 *
 * Redirecting the string when it is stored is the only point where this library
 * is still in the call.
 */
wrapper(posix_spawn_file_actions_addopen, int,
        (posix_spawn_file_actions_t * file_actions, int fd, const char * path,
         int oflag, mode_t mode))
{
    char fakechroot_abspath[FAKECHROOT_PATH_MAX];
    char fakechroot_buf[FAKECHROOT_PATH_MAX];
    debug("posix_spawn_file_actions_addopen(&file_actions, %d, \"%s\", %d, %d)",
          fd, path, oflag, mode);
    if (oflag & (O_WRONLY | O_RDWR | O_CREAT)) {
        expand_chroot_path_write(path, 0);
    } else {
        expand_chroot_path(path);
    }
    return nextcall(posix_spawn_file_actions_addopen)(file_actions, fd, path, oflag, mode);
}

#else
typedef int empty_translation_unit;
#endif
