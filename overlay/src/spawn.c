/*
    libfakechroot -- fake chroot environment

    This library is free software; you can redistribute it and/or
    modify it under the terms of the GNU Lesser General Public
    License as published by the Free Software Foundation; either
    version 2.1 of the License, or (at your option) any later version.
*/

/* Everything else that starts a program.
 *
 * Wrapping execve alone is not wrapping exec. glibc's execv, execvp, execl,
 * execlp, execle and execvpe all reach the kernel through an INTERNAL call to
 * execve that does not go through the PLT, so an LD_PRELOAD interposition of
 * execve never sees them. posix_spawn does not go near execve at all: it is a
 * clone/exec of its own inside the C library.
 *
 * The consequence is not a failure, which is what makes it expensive. A program
 * started this way runs the copy in the SHARED tree instead of the one this job
 * built, so the test passes or fails according to a binary no candidate
 * produced. MEASURED: a reduction whose criterion was a ctest test emptied
 * main() completely and ctest reported "Passed", because ctest -- which is
 * CMake, which spawns through libuv, which uses execvp -- ran the pristine
 * executable. The reduction then published a project that fails its own check.
 *
 * So the whole family is here, each one doing exactly what the execve wrapper
 * does: redirect the path into the delta when it is under a root, and keep the
 * overlay in the environment of whatever starts.
 */

#include <config.h>

#include <errno.h>
#include <spawn.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "libfakechroot.h"

extern char ** environ;

/* Defined in execve.c, which owns the environment fixup. */
char ** fakechroot_exec_env (char * const envp[]);


/* The argv of an execl-style call, collected from the varargs. */
static char ** collect_args (const char * first, va_list ap, char *** envp_out)
{
    size_t cap = 16, n = 0;
    char ** argv = (char **) malloc(cap * sizeof(char *));

    if (argv == NULL)
        return NULL;
    argv[n++] = (char *) first;
    while (argv[n - 1] != NULL) {
        if (n == cap) {
            char ** grown = (char **) realloc(argv, (cap *= 2) * sizeof(char *));
            if (grown == NULL) {
                free(argv);
                return NULL;
            }
            argv = grown;
        }
        argv[n++] = va_arg(ap, char *);
    }
    if (envp_out != NULL)
        *envp_out = va_arg(ap, char **);
    return argv;
}


wrapper(execv, int, (const char * path, char * const argv[]))
{
    debug("execv(\"%s\", ...)", path);
    return execve(path, argv, environ);
}


wrapper(execvp, int, (const char * file, char * const argv[]))
{
    debug("execvp(\"%s\", ...)", file);
    /* A bare name is looked up in PATH by the C library, and nothing in PATH is
       under a root, so only a path with a slash can need redirecting -- and
       execve is where that happens. */
    if (strchr(file, '/') != NULL)
        return execve(file, argv, environ);
    return nextcall(execvp)(file, argv);
}


wrapper(execvpe, int, (const char * file, char * const argv[], char * const envp[]))
{
    debug("execvpe(\"%s\", ...)", file);
    if (strchr(file, '/') != NULL)
        return execve(file, argv, envp);
    return nextcall(execvpe)(file, argv, envp);
}


int execl (const char * path, const char * arg, ...)
{
    va_list ap;
    char ** argv;
    int rc;

    va_start(ap, arg);
    argv = collect_args(arg, ap, NULL);
    va_end(ap);
    if (argv == NULL)
        return -1;
    rc = execve(path, argv, environ);
    free(argv);
    return rc;
}


int execlp (const char * file, const char * arg, ...)
{
    va_list ap;
    char ** argv;
    int rc;

    va_start(ap, arg);
    argv = collect_args(arg, ap, NULL);
    va_end(ap);
    if (argv == NULL)
        return -1;
    rc = execvp(file, argv);
    free(argv);
    return rc;
}


int execle (const char * path, const char * arg, ...)
{
    va_list ap;
    char ** argv;
    char ** envp = NULL;
    int rc;

    va_start(ap, arg);
    argv = collect_args(arg, ap, &envp);
    va_end(ap);
    if (argv == NULL)
        return -1;
    rc = execve(path, argv, envp);
    free(argv);
    return rc;
}


wrapper(posix_spawn, int, (pid_t * pid, const char * path,
                           const posix_spawn_file_actions_t * file_actions,
                           const posix_spawnattr_t * attrp,
                           char * const argv[], char * const envp[]))
{
    char fakechroot_abspath[FAKECHROOT_PATH_MAX];
    char fakechroot_buf[FAKECHROOT_PATH_MAX];
    char ** env;
    int rc;

    debug("posix_spawn(\"%s\", ...)", path);
    expand_chroot_path(path);

    env = fakechroot_exec_env(envp);
    rc = nextcall(posix_spawn)(pid, path, file_actions, attrp, argv,
                               env != NULL ? env : envp);
    free(env);
    return rc;
}


wrapper(posix_spawnp, int, (pid_t * pid, const char * file,
                            const posix_spawn_file_actions_t * file_actions,
                            const posix_spawnattr_t * attrp,
                            char * const argv[], char * const envp[]))
{
    char fakechroot_abspath[FAKECHROOT_PATH_MAX];
    char fakechroot_buf[FAKECHROOT_PATH_MAX];
    char ** env;
    int rc;

    debug("posix_spawnp(\"%s\", ...)", file);
    env = fakechroot_exec_env(envp);
    if (strchr(file, '/') != NULL) {
        const char * path = file;
        expand_chroot_path(path);
        rc = nextcall(posix_spawnp)(pid, path, file_actions, attrp, argv,
                                    env != NULL ? env : envp);
    } else {
        rc = nextcall(posix_spawnp)(pid, file, file_actions, attrp, argv,
                                    env != NULL ? env : envp);
    }
    free(env);
    return rc;
}
