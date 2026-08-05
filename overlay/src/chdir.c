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

#include <errno.h>
#include <unistd.h>

#include "libfakechroot.h"


/* Standing somewhere is not the same question as reading something.
 *
 * The shared path is preferred, deliberately. Every other part of this library
 * assumes the working directory is a path in the shared tree: rel2abs turns a
 * relative name into an absolute one against the real cwd, and the result is
 * then substituted. Moving the cwd into the delta whenever the delta happens to
 * hold that directory would make getcwd report a delta path, and every relative
 * name in the build -- and every path a compiler writes into a depfile from it
 * -- would carry it.
 *
 * The delta is used only when the shared tree has no such directory at all,
 * which is a directory this candidate itself created. MEASURED before this
 * existed: mkdir succeeded, stat agreed the directory was there, and chdir into
 * it failed with ENOENT, because mkdir was redirected and chdir was not. A
 * build that creates an output directory and then works inside it could not.
 *
 * Standing inside the delta does not redirect twice: the delta lies outside the
 * root, so a relative name resolved against it is left alone. MEASURED -- a
 * relative write from a delta cwd landed once, at delta/<path>, not at
 * delta/delta/<path>.
 *
 * A directory the candidate deleted must refuse, like opendir: otherwise a job
 * can stand in something it removed, and every relative name it uses from there
 * reads a tree the candidate says is gone.
 */
wrapper(chdir, int, (const char * path))
{
    char fakechroot_abspath[FAKECHROOT_PATH_MAX];
    char fakechroot_buf[FAKECHROOT_PATH_MAX];
    const char * shared;
    int retval;

    debug("chdir(\"%s\")", path);

    {
        char probe[FAKECHROOT_PATH_MAX];
        char abs[FAKECHROOT_PATH_MAX];
        rel2abs(path, abs);
        if (fakechroot_overlay_hidden(abs, probe)) {
            errno = ENOENT;
            return -1;
        }
    }

    expand_chroot_path_nodelta(path);
    shared = path;
    retval = nextcall(chdir)(shared);
    if (retval == 0 || errno != ENOENT) {
        return retval;
    }

    /* Not in the shared tree. Only this job has it, so only this job can go in. */
    expand_chroot_rel_path(path);
    if (path == shared) {
        errno = ENOENT;
        return -1;
    }
    return nextcall(chdir)(path);
}
