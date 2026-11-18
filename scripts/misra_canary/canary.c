/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- scripts/misra_canary/canary.c
 * misra canary: NOT part of the build and outside the scanned tree. check.sh misra
 * runs cppcheck with scripts/misra.json on this file first and FAILS unless
 * every forbidden-function rule of the subset fires here:
 *   21.3 malloc/free, 21.6 stdio, 21.8 abort/exit/getenv, 21.21 system.
 * A suppressed or renumbered rule (cppcheck 2.10 checks system() under
 * 21.21, not 21.8) is caught here instead of silently passing the real scan.
 */
#include <stdio.h>
#include <stdlib.h>

void ts_misra_canary(void);

void ts_misra_canary(void)
{
    char *p = malloc(16U);
    (void)printf("canary\n");
    (void)system("true");
    free(p);
    abort();
}
