/*
    libfakechroot -- fake chroot environment

    This library is free software; you can redistribute it and/or
    modify it under the terms of the GNU Lesser General Public
    License as published by the Free Software Foundation; either
    version 2.1 of the License, or (at your option) any later version.
*/


#include <config.h>

#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "libfakechroot.h"


/* Running a program the job just built.
 *
 * A reduction's interestingness test almost always ends by running something it
 * compiled -- and that binary was written into the job's delta, because that is
 * where this overlay puts everything a job produces. If exec is not redirected,
 * the shell reports "required file not found" for a file it watched the
 * compiler create moments earlier, and the test fails for a reason that has
 * nothing to do with the candidate.
 *
 * The upstream fakechroot execve is much larger than this: it emulates a chroot,
 * rewrites interpreters, and manages a chroot-relative environment. None of that
 * belongs here -- this fork is an overlay, not a chroot -- so what remains is
 * the path lookup plus one thing that is not optional: keeping the overlay
 * itself in the environment of the new program. A child that loses LD_PRELOAD
 * silently reads the pristine tree, which is the failure this whole library
 * exists to prevent, and it would happen at every exec.
 */

char ** fakechroot_exec_env (char * const envp[])
{
    static const char * const keep[] = { "LD_PRELOAD=", "CVISE_OVERLAY_DELTA=", "CVISE_OVERLAY_ROOT=" };
    size_t n = 0, i, k, extra = 0;
    char ** out;

    while (envp != NULL && envp[n] != NULL)
        n++;

    for (k = 0; k < sizeof(keep) / sizeof(keep[0]); k++) {
        size_t len = strlen(keep[k]);
        int present = 0;
        for (i = 0; i < n; i++) {
            if (strncmp(envp[i], keep[k], len) == 0) {
                present = 1;
                break;
            }
        }
        if (!present && getenv(keep[k] + 0) != NULL)
            extra++;
    }

    out = (char **) malloc((n + extra + 1) * sizeof(char *));
    if (out == NULL)
        return NULL;
    for (i = 0; i < n; i++)
        out[i] = envp[i];

    for (k = 0; k < sizeof(keep) / sizeof(keep[0]); k++) {
        size_t len = strlen(keep[k]);
        char name[64], *value, *entry;
        int present = 0;
        for (i = 0; i < n; i++) {
            if (strncmp(envp[i], keep[k], len) == 0) {
                present = 1;
                break;
            }
        }
        if (present || len >= sizeof(name))
            continue;
        memcpy(name, keep[k], len - 1);
        name[len - 1] = '\0';
        value = getenv(name);
        if (value == NULL)
            continue;
        entry = (char *) malloc(len + strlen(value) + 1);
        if (entry == NULL)
            continue;
        memcpy(entry, keep[k], len);
        strcpy(entry + len, value);
        out[n++] = entry;
    }
    out[n] = NULL;
    return out;
}


wrapper(execve, int, (const char * filename, char * const argv[], char * const envp[]))
{
    char fakechroot_abspath[FAKECHROOT_PATH_MAX];
    char fakechroot_buf[FAKECHROOT_PATH_MAX];
    char ** env;
    int rc;

    debug("execve(\"%s\", ...)", filename);
    expand_chroot_path(filename);

    env = fakechroot_exec_env(envp);
    rc = nextcall(execve)(filename, argv, env != NULL ? env : envp);
    free(env);
    return rc;
}
