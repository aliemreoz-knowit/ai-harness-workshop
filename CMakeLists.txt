# tinystream v2 -- build description.
#
# Module implementers never edit this file: every directory is globbed
# (CONFIGURE_DEPENDS), so adding a .c file under src/<module>/, tools/*,
# tests/unit/test_*.c, tests/loopback/*.c, tests/budget/*.c or fuzz/fuzz_*.c
# is enough.
# Presets live in CMakePresets.json (host, asan, fuzz, pi, coverage).
cmake_minimum_required(VERSION 3.25)
project(tinystream VERSION 2.0.0 LANGUAGES C)

option(TS_WITH_SDL   "Build tsrecv with the SDL2 live viewer"            OFF)
option(TS_BUILD_FUZZ "Build libFuzzer targets (needs clang)"             OFF)
option(TS_FUZZ_ONLY  "Build only the libraries and the fuzz targets"     OFF)
option(TS_COVERAGE   "Coverage build (flags come from the preset)"       OFF)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE OFF)

# ---- Warning sets --------------------------------------------------------------
# ts_warnings: every file we own (libs, tools, tests).
# ts_strict:   libraries additionally require a prototype for every function.
add_library(ts_warnings INTERFACE)
target_compile_options(ts_warnings INTERFACE
    -Wall -Wextra -Wpedantic -Werror
    -Wshadow -Wconversion -Wsign-conversion -Wstrict-prototypes
    -Wvla -Wundef -Wformat=2 -Wcast-qual -Wdouble-promotion)
target_compile_definitions(ts_warnings INTERFACE _DEFAULT_SOURCE _POSIX_C_SOURCE=200809L)

add_library(ts_strict INTERFACE)
target_link_libraries(ts_strict INTERFACE ts_warnings)
target_compile_options(ts_strict INTERFACE -Wmissing-prototypes -Wmissing-declarations)

# ---- Libraries -------------------------------------------------------------------
# tsproto: wire format + codec, shared by daemon and receiver.
file(GLOB TS_PROTO_SRC CONFIGURE_DEPENDS src/proto/*.c src/codec/*.c)
add_library(tsproto STATIC ${TS_PROTO_SRC})
target_include_directories(tsproto PUBLIC ${CMAKE_SOURCE_DIR}/include)
target_link_libraries(tsproto PRIVATE ts_strict)
# -fstack-usage: gate budgets reads the .su files of every daemon object (max frame budget).
target_compile_options(tsproto PRIVATE -fstack-usage)

# tsd: daemon modules (no stdio in hot paths, no malloc, log compiled out).
file(GLOB TS_D_SRC CONFIGURE_DEPENDS
    src/txq/*.c src/rate/*.c src/clients/*.c src/net/*.c
    src/pool/*.c src/capture/*.c src/io/*.c src/log/*.c)
add_library(tsd STATIC ${TS_D_SRC})
target_link_libraries(tsd PUBLIC tsproto PRIVATE ts_strict)
target_compile_definitions(tsd PRIVATE TS_LOG_ENABLED=0)
target_compile_options(tsd PRIVATE -fstack-usage)

# tsr: receiver core (tools/tsrecv/lib).
file(GLOB TS_R_SRC CONFIGURE_DEPENDS tools/tsrecv/lib/*.c)
add_library(tsr STATIC ${TS_R_SRC})
target_link_libraries(tsr PUBLIC tsd tsproto PRIVATE ts_strict)
target_compile_definitions(tsr PRIVATE TS_LOG_ENABLED=1)

# tsnetem_shim: the chaos filter as an in-process library.
file(GLOB TS_NETEM_SRC CONFIGURE_DEPENDS tools/tsnetem/lib/*.c)
add_library(tsnetem_shim STATIC ${TS_NETEM_SRC})
target_link_libraries(tsnetem_shim PUBLIC tsd tsproto PRIVATE ts_strict)
target_compile_definitions(tsnetem_shim PRIVATE TS_LOG_ENABLED=1)

# ---- Fuzz targets (only in the fuzz preset) -------------------------------------
if(TS_BUILD_FUZZ)
    file(GLOB TS_FUZZ_SRC CONFIGURE_DEPENDS fuzz/fuzz_*.c)
    foreach(src ${TS_FUZZ_SRC})
        get_filename_component(name ${src} NAME_WE)
        add_executable(${name} ${src})
        target_link_libraries(${name} PRIVATE tsr tsnetem_shim tsd tsproto ts_warnings)
        target_compile_options(${name} PRIVATE -fsanitize=fuzzer)
        target_link_options(${name} PRIVATE -fsanitize=fuzzer)
        target_compile_definitions(${name} PRIVATE TS_LOG_ENABLED=1)
    endforeach()
endif()

if(TS_FUZZ_ONLY)
    return()
endif()

# ---- Executables -----------------------------------------------------------------
# tinystreamd: main.c + malloc_guard.c; any allocation after ts_pool_seal() aborts.
file(GLOB TS_MAIN_SRC CONFIGURE_DEPENDS src/*.c)
add_executable(tinystreamd ${TS_MAIN_SRC})
target_link_libraries(tinystreamd PRIVATE tsd tsproto ts_strict)
target_compile_definitions(tinystreamd PRIVATE TS_LOG_ENABLED=0)
target_link_options(tinystreamd PRIVATE
    -Wl,--wrap=malloc -Wl,--wrap=calloc -Wl,--wrap=realloc -Wl,--wrap=free)
target_compile_options(tinystreamd PRIVATE -fstack-usage)

# tsrecv: viewer / metrics tool.
file(GLOB TS_RECV_SRC CONFIGURE_DEPENDS tools/tsrecv/*.c)
add_executable(tsrecv ${TS_RECV_SRC})
target_link_libraries(tsrecv PRIVATE tsr ts_warnings)
target_compile_definitions(tsrecv PRIVATE TS_LOG_ENABLED=1)
if(TS_WITH_SDL)
    find_package(SDL2 REQUIRED)
    target_link_libraries(tsrecv PRIVATE SDL2::SDL2)
    target_compile_definitions(tsrecv PRIVATE TS_WITH_SDL=1)
endif()

# tsnetem: userspace UDP chaos relay.
file(GLOB TS_NETEM_TOOL_SRC CONFIGURE_DEPENDS tools/tsnetem/*.c)
add_executable(tsnetem ${TS_NETEM_TOOL_SRC})
target_link_libraries(tsnetem PRIVATE tsnetem_shim ts_warnings)
target_compile_definitions(tsnetem PRIVATE TS_LOG_ENABLED=1)

# ---- Tests ------------------------------------------------------------------------
enable_testing()

add_library(unity STATIC third_party/unity/unity.c)
target_include_directories(unity PUBLIC ${CMAKE_SOURCE_DIR}/third_party/unity)
target_compile_definitions(unity PUBLIC UNITY_INCLUDE_DOUBLE UNITY_OUTPUT_COLOR=0)
target_compile_options(unity PRIVATE -w)

# tests/support: loopback harness glue (virtual clock, fake io, netem shim).
file(GLOB TS_SUPPORT_SRC CONFIGURE_DEPENDS tests/support/*.c tests/vectors/*.c)
add_library(ts_testsupport STATIC ${TS_SUPPORT_SRC})
target_include_directories(ts_testsupport PUBLIC ${CMAKE_SOURCE_DIR}/tests/support ${CMAKE_SOURCE_DIR}/tests/vectors)
target_link_libraries(ts_testsupport PUBLIC tsr tsnetem_shim tsd tsproto unity PRIVATE ts_strict)
target_compile_definitions(ts_testsupport PRIVATE TS_LOG_ENABLED=1)

function(ts_add_tests glob label)
    file(GLOB srcs CONFIGURE_DEPENDS ${glob})
    foreach(src ${srcs})
        get_filename_component(name ${src} NAME_WE)
        add_executable(${name} ${src})
        target_link_libraries(${name} PRIVATE ts_testsupport ts_warnings)
        target_compile_definitions(${name} PRIVATE TS_LOG_ENABLED=1)
        add_test(NAME ${name} COMMAND ${name})
        set(labels "${label}")
        if(name MATCHES "^test_prop_")
            list(APPEND labels property)
        endif()
        set_tests_properties(${name} PROPERTIES LABELS "${labels}" TIMEOUT 120)
    endforeach()
endfunction()

ts_add_tests("${CMAKE_SOURCE_DIR}/tests/unit/test_*.c" unit)
ts_add_tests("${CMAKE_SOURCE_DIR}/tests/loopback/*.c" loopback)
# budgets (ratio, PSNR, TS_ENC_MAX); size and stack budgets are checked by scripts/check.sh budgets.
ts_add_tests("${CMAKE_SOURCE_DIR}/tests/budget/*.c" budget)
