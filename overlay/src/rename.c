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

#include "libfakechroot.h"


wrapper(rename, int, (const char * oldpath, const char * newpath))
{
    char fakechroot_abspath[FAKECHROOT_PATH_MAX];
    char fakechroot_buf[FAKECHROOT_PATH_MAX];
    char tmp[FAKECHROOT_PATH_MAX];
    char oldabs[FAKECHROOT_PATH_MAX];
    char hidebuf[FAKECHROOT_PATH_MAX];
    int rc;

    debug("rename(\"%s\", \"%s\")", oldpath, newpath);

    /* The name to hide is the one the CALLER used, so it has to be taken
       before the overlay rewrites the argument into the delta. */
    rel2abs(oldpath, oldabs);

    expand_chroot_path_write(oldpath, 0);
    strcpy(tmp, oldpath);
    oldpath = tmp;
    expand_chroot_path_write(newpath, 0);

    rc = nextcall(rename)(oldpath, newpath);
    /* Without this the file is still readable under its old name, straight
       from the shared original, and the rename had no effect the reduction
       can observe. */
    if (rc == 0 && getenv("CVISE_OVERLAY_DELTA") != NULL)
        fakechroot_overlay_hide(oldabs, hidebuf);
    return rc;
}
