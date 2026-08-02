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

#if !defined(OPENDIR_CALLS___OPEN) && !defined(OPENDIR_CALLS___OPENDIR2)

#include <dirent.h>
#include <errno.h>
#include "libfakechroot.h"


wrapper(opendir, DIR *, (const char * name))
{
    char fakechroot_abspath[FAKECHROOT_PATH_MAX];
    debug("opendir(\"%s\")", name);
    /* Directories are NOT served from the delta.
       A delta directory holds only what this job happened to write, so
       redirecting the listing answers "what is in here?" with "the two object
       files I just produced" and hides every source file in the shared tree.
       That is a silently wrong answer to the one question a build asks about a
       directory, and it fires on the first write into it. Listing the original
       is the honest half-answer: everything shared is visible, a file this job
       created is reachable by name even though it is not listed, and a file it
       deleted is listed but opens as ENOENT. Merging the two listings is the
       full answer and wants a readdir that walks both. */
    /* A directory the candidate deleted must not open. The listing itself is
       still the shared one -- merging both is a larger change -- but a
       whiteouted directory has to answer ENOENT, or its children stay readable
       and a deletion that "worked" changes nothing the build can see. */
    {
        char probe[FAKECHROOT_PATH_MAX];
        char abs[FAKECHROOT_PATH_MAX];
        rel2abs(name, abs);
        if (fakechroot_overlay_hidden(abs, probe)) {
            errno = ENOENT;
            return NULL;
        }
    }
    expand_chroot_path_nodelta(name);
    return nextcall(opendir)(name);
}

#else
typedef int empty_translation_unit;
#endif
