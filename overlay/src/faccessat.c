/*
    libfakechroot -- fake chroot environment
    Copyright (c) 2010, 2011 Piotr Roszatycki <dexter@debian.org>

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

/* Guarded by its own name, not by a neighbour's. This read HAVE_FCHMODAT,
   which happens to be defined on this libc, so the wrapper was built by
   accident; on a libc that has fchmodat and not faccessat it would have failed
   to compile, and on one with the reverse it would have silently gone missing
   -- and a missing wrapper here means a job asking "does this file exist" gets
   the answer for the pristine tree. */
#ifdef HAVE_FACCESSAT

#define _ATFILE_SOURCE
#include <unistd.h>
#include "libfakechroot.h"


wrapper(faccessat, int, (int dirfd, const char * pathname, int mode, int flags))
{
    char fakechroot_abspath[FAKECHROOT_PATH_MAX];
    char fakechroot_buf[FAKECHROOT_PATH_MAX];
    debug("faccessat(%d, \"%s\", %d, %d)", dirfd, pathname, mode, flags);
    expand_chroot_path_at(dirfd, pathname);
    return nextcall(faccessat)(dirfd, pathname, mode, flags);
}

#else
typedef int empty_translation_unit;
#endif
