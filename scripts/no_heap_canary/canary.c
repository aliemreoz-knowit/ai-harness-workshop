/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- scripts/no_heap_canary/canary.c
 * no-heap canary: NOT part of the build. check.sh no-heap compiles this file to an
 * object first and FAILS unless `nm -u` on it trips the forbidden-symbol list
 * for every name below: allocation (malloc), process creation and program
 * loading (exec*, fork, vfork, posix_spawn*, popen, system, dlopen). A list
 * that no longer matches (renamed, typo, versioned symbol) is caught here
 * instead of silently passing the daemon scan.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <spawn.h>
#include <dlfcn.h>

extern char **environ;
void ts_no_heap_canary(void);

void ts_no_heap_canary(void)
{
    char *argv[] = { "true", NULL };
    pid_t pid;
    void *p = malloc(16U);
    FILE *f = popen("true", "r");
    (void)system("true");
    (void)execl("/bin/true", "true", (char *)NULL);
    (void)execle("/bin/true", "true", (char *)NULL, environ);
    (void)execlp("true", "true", (char *)NULL);
    (void)execv("/bin/true", argv);
    (void)execve("/bin/true", argv, environ);
    (void)execvp("true", argv);
    (void)execvpe("true", argv, environ);
    (void)fexecve(0, argv, environ);
    (void)fork();
    (void)vfork();
    (void)posix_spawn(&pid, "/bin/true", NULL, NULL, argv, environ);
    (void)posix_spawnp(&pid, "true", NULL, NULL, argv, environ);
    (void)dlopen("libc.so.6", RTLD_NOW);
    (void)f;
    (void)p;
}
