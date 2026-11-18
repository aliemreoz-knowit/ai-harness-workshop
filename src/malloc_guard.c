/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/malloc_guard.c
 * Linked into tinystreamd only, with -Wl,--wrap=malloc,calloc,realloc,free.
 * Before ts_pool_seal() the real allocator is used (libc start-up, argument
 * parsing); afterwards any call aborts, which is what gate no-heap and the design
 * ("no dynamic memory after init") demand. Allowed to use stdlib: not a hot path.
 */
#include <stddef.h>
#include <stdlib.h>
#include "ts_pool.h"

void *__real_malloc(size_t n);
void *__real_calloc(size_t n, size_t m);
void *__real_realloc(void *p, size_t n);
void  __real_free(void *p);

void *__wrap_malloc(size_t n);
void *__wrap_calloc(size_t n, size_t m);
void *__wrap_realloc(void *p, size_t n);
void  __wrap_free(void *p);

static void guard_check(void)
{
    if (ts_pool_sealed(ts_pool_get())) {
        abort();
    }
}

void *__wrap_malloc(size_t n)
{
    guard_check();
    return __real_malloc(n);
}

void *__wrap_calloc(size_t n, size_t m)
{
    guard_check();
    return __real_calloc(n, m);
}

void *__wrap_realloc(void *p, size_t n)
{
    guard_check();
    return __real_realloc(p, n);
}

void __wrap_free(void *p)
{
    guard_check();
    __real_free(p);
}
