/*
    libfakechroot -- fake chroot environment
    Copyright (c) 2010-2015 Piotr Roszatycki <dexter@debian.org>

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

/* Not `&& !defined(HAVE___LXSTAT64)`, which is what stood here.
 *
 * The assumption was that a libc with __lxstat64 spells lstat64 as an inline
 * calling it, so wrapping both would be redundant. That has not been true since
 * glibc 2.33: lstat64 is an exported function in its own right, and __lxstat64
 * survives only as a compat symbol for older binaries. A libc has both, and
 * different programs in the same build call different ones.
 *
 * MEASURED the moment the legacy family was enabled: eleven of twelve ways of
 * asking whether a file exists were redirected and lstat64 was not, because
 * enabling __lxstat64 compiled this file out. Its siblings -- stat.c, stat64.c,
 * lstat.c, fstatat.c -- carry no such exclusion, and this one was alone in it.
 */
#ifdef HAVE_LSTAT64

#define _LARGEFILE64_SOURCE
#define _BSD_SOURCE
#define _DEFAULT_SOURCE
#include <sys/stat.h>
#include <limits.h>
#include <stdlib.h>
#include <unistd.h>

#include "libfakechroot.h"


wrapper(lstat64, int, (const char * file_name, struct stat64 * buf))
{
    char fakechroot_abspath[FAKECHROOT_PATH_MAX];
    char fakechroot_buf[FAKECHROOT_PATH_MAX];

    debug("lstat64(\"%s\", &buf)", file_name);

    /* The overlay substitution matters here as much as anywhere: this call is
       how a tool asks "does this file exist, and what is it?", and answering
       from the shared tree for a file this job replaced or deleted is a wrong
       answer to the question a reduction is built on. */
    expand_chroot_path(file_name);

    return nextcall(lstat64)(file_name, buf);
}

#else
typedef int empty_translation_unit;
#endif
