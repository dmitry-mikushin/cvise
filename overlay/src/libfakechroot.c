/*
    libfakechroot -- fake chroot environment
    Copyright (c) 2003-2015 Piotr Roszatycki <dexter@debian.org>
    Copyright (c) 2007 Mark Eichin <eichin@metacarta.com>
    Copyright (c) 2006, 2007 Alexander Shishkin <virtuoso@slind.org>

    klik2 support -- give direct access to a list of directories
    Copyright (c) 2006, 2007 Lionel Tricon <lionel.tricon@free.fr>

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

#define _GNU_SOURCE

#include <stdarg.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <pwd.h>
#include <dlfcn.h>

#include "libfakechroot.h"
#include "getcwd_real.h"
#include "strchrnul.h"

#define EXCLUDE_LIST_SIZE 100


/* Useful to exclude a list of directories or files */
static char *exclude_list[EXCLUDE_LIST_SIZE];
static size_t exclude_length[EXCLUDE_LIST_SIZE];
static int list_max = 0;
static int first = 0;


/* List of environment variables to preserve on clearenv() */
char *preserve_env_list[] = {
    "FAKECHROOT_BASE",
    "FAKECHROOT_CMD_SUBST",
    "FAKECHROOT_DEBUG",
    "FAKECHROOT_DETECT",
    "FAKECHROOT_ELFLOADER",
    "FAKECHROOT_ELFLOADER_OPT_ARGV0",
    "FAKECHROOT_EXCLUDE_PATH",
    "FAKECHROOT_LDLIBPATH",
    "FAKECHROOT_VERSION",
    "FAKEROOTKEY",
    "FAKED_MODE",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD"
};
const int preserve_env_list_count = sizeof preserve_env_list / sizeof preserve_env_list[0];


LOCAL int fakechroot_debug (const char *fmt, ...)
{
    int ret;
    char newfmt[2048];

    va_list ap;
    va_start(ap, fmt);

    if (!getenv("FAKECHROOT_DEBUG"))
        return 0;

    sprintf(newfmt, PACKAGE ": %s\n", fmt);

    ret = vfprintf(stderr, newfmt, ap);
    va_end(ap);

    return ret;
}


#include "getcwd.h"


/* Bootstrap the library */
void fakechroot_init (void) CONSTRUCTOR;
void fakechroot_init (void)
{
    char *detect = getenv("FAKECHROOT_DETECT");


    if (detect) {
        /* printf causes coredump on FreeBSD */
        if (write(STDOUT_FILENO, PACKAGE, sizeof(PACKAGE)-1) &&
            write(STDOUT_FILENO, " ", 1) &&
            write(STDOUT_FILENO, VERSION, sizeof(VERSION)-1) &&
            write(STDOUT_FILENO, "\n", 1)) { /* -Wunused-result */ }
        _Exit(atoi(detect));
    }

    debug("fakechroot_init()");
    debug("FAKECHROOT_BASE=\"%s\"", getenv("FAKECHROOT_BASE"));
    debug("FAKECHROOT_BASE_ORIG=\"%s\"", getenv("FAKECHROOT_BASE_ORIG"));
    debug("FAKECHROOT_CMD_ORIG=\"%s\"", getenv("FAKECHROOT_CMD_ORIG"));

    if (!first) {
        char *exclude_path = getenv("FAKECHROOT_EXCLUDE_PATH");

        first = 1;

        /* We get a list of directories or files */
        if (exclude_path) {
            int i;
            for (i = 0; list_max < EXCLUDE_LIST_SIZE; ) {
                int j;
                for (j = i; exclude_path[j] != ':' && exclude_path[j] != '\0'; j++);
                exclude_list[list_max] = malloc(j - i + 2);
                memset(exclude_list[list_max], '\0', j - i + 2);
                strncpy(exclude_list[list_max], &(exclude_path[i]), j - i);
                exclude_length[list_max] = strlen(exclude_list[list_max]);
                list_max++;
                if (exclude_path[j] != ':') break;
                i = j + 1;
            }
        }

            }
}


/* Lazily load function */
LOCAL fakechroot_wrapperfn_t fakechroot_loadfunc (struct fakechroot_wrapper * w)
{
    char *msg;
    if (!(w->nextfunc = dlsym(RTLD_NEXT, w->name))) {;
        msg = dlerror();
        fprintf(stderr, "%s: %s: %s\n", PACKAGE, w->name, msg != NULL ? msg : "unresolved symbol");
        exit(EXIT_FAILURE);
    }
    return w->nextfunc;
}


/* Existence check that must not go through our own wrappers. */
static int overlay_exists (const char * path)
{
    static int (*real_access)(const char *, int) = NULL;

    if (real_access == NULL) {
        real_access = (int (*)(const char *, int)) dlsym(RTLD_NEXT, "access");
        if (real_access == NULL)
            return 0;
    }
    return real_access(path, F_OK) == 0;
}


/* Compose the delta path for an absolute path.  Returns 0 if there is no
   delta configured or the name would not fit. */
static int overlay_delta_path (const char * path, char * buf)
{
    const char *delta, *root;
    size_t delta_len, path_len, root_len;

    if (!first)
        fakechroot_init();

    delta = getenv("CVISE_OVERLAY_DELTA");
    if (delta == NULL || *delta == '\0')
        return 0;

    /* Only the trees under reduction are overlaid.  Redirecting EVERY absolute
       path is not a bigger version of the same idea, it is a different and
       broken one: the job also opens its own sockets, /proc, /dev and the
       runtime scratch of whatever language its tools are written in, and
       sending those into the delta breaks them in ways that have nothing to do
       with the reduction.  Python's forkserver, for one, creates a directory
       and binds a socket in it -- the directory went to the delta, bind() is
       not a path call we intercept, and C-Vise died before it ran a single
       test.

       There is more than one such tree, and the second one is not optional.
       The sources are what a candidate changes; the build directory is where
       the answer about it is computed -- the objects, the link, and ninja's own
       record of what is up to date.  Left shared, it hands each job whatever
       the previous one built: a file this candidate did not touch is read from
       the pristine tree with the pristine timestamp, which is older than the
       object the previous candidate left behind, so it counts as up to date and
       is linked as it stands.  The verdict is then about a program no candidate
       ever described.  The roots are given colon-separated, the way a search
       path is. */
    root = getenv("CVISE_OVERLAY_ROOT");
    if (root != NULL && *root != '\0') {
        const char *start = root;
        int covered = 0;

        while (*start != '\0') {
            const char *end = strchr(start, ':');
            root_len = end != NULL ? (size_t) (end - start) : strlen(start);
            while (root_len > 1 && start[root_len - 1] == '/')
                root_len--;
            if (root_len > 0 && strncmp(path, start, root_len) == 0 &&
                (path[root_len] == '\0' || path[root_len] == '/')) {
                covered = 1;
                break;
            }
            if (end == NULL)
                break;
            start = end + 1;
        }
        if (!covered)
            return 0;
    }

    delta_len = strlen(delta);
    path_len = strlen(path);
    if (delta_len + path_len + sizeof(FAKECHROOT_WHITEOUT_SUFFIX) + sizeof(CVISE_ABSENT_DIR) >= FAKECHROOT_PATH_MAX)
        return 0;

    memcpy(buf, delta, delta_len);
    memcpy(buf + delta_len, path, path_len + 1);
    return 1;
}


/* mkdir -p for the parents of a delta path, bypassing our own wrappers. */
static void overlay_make_parents (char * buf)
{
    static int (*real_mkdir)(const char *, mode_t) = NULL;
    char *p;

    if (real_mkdir == NULL) {
        real_mkdir = (int (*)(const char *, mode_t)) dlsym(RTLD_NEXT, "mkdir");
        if (real_mkdir == NULL)
            return;
    }

    for (p = buf + 1; *p; p++) {
        if (*p != '/')
            continue;
        *p = '\0';
        real_mkdir(buf, 0755);
        *p = '/';
    }
}


/* Bring the original file into the delta, so that a writer which does not
   truncate does not start from an empty file. */
static int overlay_copy_up (const char * original, const char * target)
{
    static int (*real_open)(const char *, int, ...) = NULL;
    static ssize_t (*real_read)(int, void *, size_t) = NULL;
    static ssize_t (*real_write)(int, const void *, size_t) = NULL;
    static int (*real_close)(int) = NULL;
    static int (*real_fstat)(int, struct stat *) = NULL;
    static int (*real_fchmod)(int, mode_t) = NULL;
    static int (*real_mkdir)(const char *, mode_t) = NULL;
    static int (*real_unlink)(const char *) = NULL;
    struct stat st;
    char chunk[65536];
    int in, out;
    ssize_t n;

    if (real_open == NULL) {
        real_open   = (int (*)(const char *, int, ...)) dlsym(RTLD_NEXT, "open");
        real_read   = (ssize_t (*)(int, void *, size_t)) dlsym(RTLD_NEXT, "read");
        real_write  = (ssize_t (*)(int, const void *, size_t)) dlsym(RTLD_NEXT, "write");
        real_close  = (int (*)(int)) dlsym(RTLD_NEXT, "close");
        real_fstat  = (int (*)(int, struct stat *)) dlsym(RTLD_NEXT, "fstat");
        real_fchmod = (int (*)(int, mode_t)) dlsym(RTLD_NEXT, "fchmod");
        real_mkdir  = (int (*)(const char *, mode_t)) dlsym(RTLD_NEXT, "mkdir");
        real_unlink = (int (*)(const char *)) dlsym(RTLD_NEXT, "unlink");
        if (real_open == NULL || real_read == NULL || real_write == NULL ||
            real_close == NULL || real_fstat == NULL || real_mkdir == NULL)
            return -1;
    }

    in = real_open(original, O_RDONLY);
    if (in < 0) {
        debug("overlay_copy_up: cannot read %s", original);
        return -1;
    }
    /* A directory opens fine and then reads as EISDIR, which used to leave an
       empty regular file where a directory belongs -- and every later
       operation on it failed with ENOTDIR for reasons nothing explained. */
    if (real_fstat(in, &st) == 0 && S_ISDIR(st.st_mode)) {
        real_close(in);
        return real_mkdir(target, st.st_mode & 07777) == 0 ? 0 : -1;
    }

    out = real_open(target, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (out < 0) {
        debug("overlay_copy_up: cannot create %s", target);
        real_close(in);
        return -1;
    }
    while ((n = real_read(in, chunk, sizeof(chunk))) > 0) {
        ssize_t off = 0;
        while (off < n) {
            ssize_t w = real_write(out, chunk + off, (size_t)(n - off));
            if (w <= 0) {
                /* A half-copied file is worse than none: the caller would
                   append to a truncated original and never learn. */
                debug("overlay_copy_up: short write to %s", target);
                real_close(in);
                real_close(out);
                if (real_unlink != NULL)
                    real_unlink(target);
                return -1;
            }
            off += w;
        }
    }
    if (n < 0) {
        debug("overlay_copy_up: read error on %s", original);
        real_close(in);
        real_close(out);
        if (real_unlink != NULL)
            real_unlink(target);
        return -1;
    }
    /* Keep the mode, or an executable copied up stops being executable and the
       build fails somewhere far away from here. */
    if (real_fchmod != NULL)
        real_fchmod(out, st.st_mode & 07777);
    real_close(in);
    real_close(out);
    return 0;
}


/* Was this path deleted by the candidate?

   Distinct from "the delta has something here": after a job writes one file
   into a directory, the delta contains that directory too, and treating that
   as a deletion would make every directory the job has written into vanish. */
LOCAL int fakechroot_overlay_hidden (const char * path, char * buf)
{
    size_t len;

    if (!overlay_delta_path(path, buf))
        return 0;
    len = strlen(buf);
    memcpy(buf + len, FAKECHROOT_WHITEOUT_SUFFIX, sizeof(FAKECHROOT_WHITEOUT_SUFFIX));
    return overlay_exists(buf);
}


/* Hide a path: the test case deleted it.

   Deleting through the overlay cannot mean deleting the original -- the
   original is shared by every parallel job and is the pristine tree. Nor can it
   mean deleting the delta copy and stopping there, because then the original
   simply reappears through the read path. So a deletion is recorded as a
   marker, and the read path turns that marker into ENOENT for everyone who
   looks. Without this, a reduction that removes a file has no effect at all,
   and the run silently concludes the file was load-bearing. */
LOCAL int fakechroot_overlay_hide (const char * path, char * buf)
{
    static int (*real_unlink)(const char *) = NULL;
    static int (*real_open)(const char *, int, ...) = NULL;
    static int (*real_close)(int) = NULL;
    size_t len;
    int fd, existed;

    if (real_unlink == NULL) {
        real_unlink = (int (*)(const char *)) dlsym(RTLD_NEXT, "unlink");
        real_open   = (int (*)(const char *, int, ...)) dlsym(RTLD_NEXT, "open");
        real_close  = (int (*)(int)) dlsym(RTLD_NEXT, "close");
        if (real_unlink == NULL || real_open == NULL || real_close == NULL)
            return -1;
    }

    if (!overlay_delta_path(path, buf))
        return -1;

    /* Whether anything was there at all decides what the caller is told. */
    existed = overlay_exists(buf) || overlay_exists(path);

    real_unlink(buf);                    /* drop the job's own copy, if any */
    overlay_make_parents(buf);

    len = strlen(buf);
    memcpy(buf + len, FAKECHROOT_WHITEOUT_SUFFIX, sizeof(FAKECHROOT_WHITEOUT_SUFFIX));
    fd = real_open(buf, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    buf[len] = '\0';
    if (fd < 0)
        return -1;
    real_close(fd);

    if (!existed) {
        errno = ENOENT;
        return -1;
    }
    return 0;
}


/* Serve a path that is about to be WRITTEN out of the delta.

   Reads are cheap to redirect because the delta either holds the file or it
   does not.  Writes are the other half of an overlay, and without them every
   job needs its own copy of everything the build might write -- which for this
   project is 1.1 GB of build directory per job, in RAM, for the sake of the
   few object files the job actually rebuilds.

   So a write goes to the delta instead: the parents are created there, the
   original is copied up when the writer does not truncate, and the caller
   never learns that it is not writing where it asked to.  What the job did not
   rebuild is still read from the shared original. */
LOCAL const char * fakechroot_overlay_path_write (const char * path, char * buf, int truncating)
{
    char whiteout[FAKECHROOT_PATH_MAX];

    if (!overlay_delta_path(path, buf))
        return path;

    if (overlay_exists(buf))
        return buf;

    overlay_make_parents(buf);

    /* A path this candidate DELETED starts empty, never from the original.
     *
     * Copying up here would fetch the shared tree's version of a file the
     * candidate removed, and the writer would then be extending content its
     * own candidate does not contain. MEASURED before this check: delete a
     * file, reopen it O_WRONLY|O_CREAT without O_TRUNC, write three bytes --
     *
     *     'NEWGINAL-CONTENT-THAT-THE-CANDIDATE-DELETED'
     *
     * the deletion silently undone underneath. The symptom that led here was
     * milder and easy to dismiss: O_CREAT|O_EXCL over a deleted path returned
     * EEXIST, because the copy-up had just put the file back. */
    if (fakechroot_overlay_hidden(path, whiteout))
        return buf;

    if (!truncating && overlay_exists(path) && overlay_copy_up(path, buf) != 0) {
        /* Nothing was copied up, so writing into the delta would start from an
           empty file and quietly lose what was there. Let the caller work on
           the original path instead and fail honestly if it cannot. */
        return path;
    }

    return buf;
}


/* Serve a path out of the delta of the current test case.

   CVISE_OVERLAY_DELTA names a directory that mirrors absolute paths.  If it
   holds a file for this path, that file is used instead; if it holds a
   whiteout marker, the path is reported as absent; otherwise the original path
   is returned untouched, which is the case for almost every file and is why
   this costs nothing.  No tree is copied and no mtime is disturbed. */
LOCAL const char * fakechroot_overlay_path (const char * path, char * buf)
{
    const char *delta;
    size_t delta_len, path_len;

    if (!first)
        fakechroot_init();

    delta = getenv("CVISE_OVERLAY_DELTA");
    if (delta == NULL || *delta == '\0')
        return path;

    delta_len = strlen(delta);
    path_len = strlen(path);
    if (delta_len + path_len + sizeof(FAKECHROOT_WHITEOUT_SUFFIX) + sizeof(CVISE_ABSENT_DIR) >= FAKECHROOT_PATH_MAX)
        return path;

    /* A file the test case replaced. */
    memcpy(buf, delta, delta_len);
    memcpy(buf + delta_len, path, path_len + 1);
    if (overlay_exists(buf))
        return buf;

    /* A file the test case removed: point at the marker, which does not exist
       as a regular path, so the caller gets ENOENT for the original. */
    memcpy(buf + delta_len + path_len, FAKECHROOT_WHITEOUT_SUFFIX,
           sizeof(FAKECHROOT_WHITEOUT_SUFFIX));
    if (overlay_exists(buf)) {
        /* Point at something under a directory that never exists, so the
           caller gets a clean ENOENT. Appending to the path itself would claim
           the path is a directory and produce ENOTDIR instead, which is a
           different answer to a different question. */
        memcpy(buf, delta, delta_len);
        memcpy(buf + delta_len, CVISE_ABSENT_DIR, sizeof(CVISE_ABSENT_DIR) - 1);
        memcpy(buf + delta_len + sizeof(CVISE_ABSENT_DIR) - 1, path, path_len + 1);
        return buf;
    }

    return path;
}


/* Proof, to whoever is about to rely on this overlay, that it is really here.

   A reduction driven through this library is only correct if the library is
   actually loaded into the processes that read the sources, and if its
   redirection is actually configured. Neither is visible from outside: a
   missing LD_PRELOAD, a static binary, a wrapper that resets the environment,
   or an unset CVISE_OVERLAY_DELTA all look exactly like a working setup right
   up to the point where the compiler silently reads the ORIGINAL sources and
   every verdict in the run becomes meaningless.

   So the caller asks, and this answers two independent things at once:

     * that this code ran at all -- the answer depends on the caller's
       challenge, so a stub returning a constant cannot forge it, and a missing
       symbol cannot be mistaken for a negative answer;
     * that the redirection is live -- the answer differs by one depending on
       whether a probe path actually gets rewritten to the delta.

   Expected answer: (challenge ^ CVISE_OVERLAY_MAGIC) + 1 with the delta in
   place; the same value without the + 1 means the library is loaded but the
   overlay is not doing anything. */
uint64_t cvise_overlay_selfcheck (uint64_t challenge)
{
    char buf[FAKECHROOT_PATH_MAX];
    const char * probe = CVISE_OVERLAY_PROBE_PATH;
    const char * answer = fakechroot_overlay_path(probe, buf);
    uint64_t redirected = (answer != probe) ? 1 : 0;

    return (challenge ^ CVISE_OVERLAY_MAGIC) + redirected;
}


/* Check if path is on exclude list */
LOCAL int fakechroot_localdir (const char * p_path)
{
    char *v_path = (char *)p_path;
    char cwd_path[FAKECHROOT_PATH_MAX];

    if (!p_path)
        return 0;

    if (!first)
        fakechroot_init();

    /* We need to expand relative paths */
    if (p_path[0] != '/') {
        getcwd_real(cwd_path, FAKECHROOT_PATH_MAX);
        v_path = cwd_path;
        narrow_chroot_path(v_path);
    }

    /* We try to find if we need direct access to a file */
    {
        const size_t len = strlen(v_path);
        int i;

        for (i = 0; i < list_max; i++) {
            if (exclude_length[i] > len ||
                    v_path[exclude_length[i] - 1] != (exclude_list[i])[exclude_length[i] - 1] ||
                    strncmp(exclude_list[i], v_path, exclude_length[i]) != 0) continue;
            if (exclude_length[i] == len || v_path[exclude_length[i]] == '/') return 1;
        }
    }

    return 0;
}


/*
 * Parse the FAKECHROOT_CMD_SUBST environment variable (the first
 * parameter) and if there is a match with filename, return the
 * substitution in cmd_subst.  Returns non-zero if there was a match.
 *
 * FAKECHROOT_CMD_SUBST=cmd=subst:cmd=subst:...
 */
LOCAL int fakechroot_try_cmd_subst (char * env, const char * filename, char * cmd_subst)
{
    int len, len2;
    char *p;

    if (env == NULL || filename == NULL)
        return 0;

    /* Remove trailing dot from filename */
    if (filename[0] == '.' && filename[1] == '/')
        filename++;
    len = strlen(filename);

    do {
        p = strchrnul(env, ':');

        if (strncmp(env, filename, len) == 0 && env[len] == '=') {
            len2 = p - &env[len+1];
            if (len2 >= FAKECHROOT_PATH_MAX)
                len2 = FAKECHROOT_PATH_MAX - 1;
            strncpy(cmd_subst, &env[len+1], len2);
            cmd_subst[len2] = '\0';
            return 1;
        }

        env = p;
    } while (*env++ != '\0');

    return 0;
}
