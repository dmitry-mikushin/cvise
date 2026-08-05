# The reduction overlay, built here rather than fetched from somewhere.
#
# This is what lets a job see its own candidate at the project's real path while
# sharing one pristine tree with every other job. Without it a reduction copies
# the whole project per candidate, which restamps every file and makes the build
# system rebuild everything -- so for a project of any size the reduction simply
# does not finish.
#
# It began as fakechroot and is no longer that program: 153 wrapped functions
# became the ~60 a compiler actually calls, chroot emulation is gone, and what
# was added -- delta redirection, whiteouts, copy-up, a self-check the reducer
# can interrogate -- exists only for reductions. Keeping it as a separate
# autotools project meant C-Vise depended on someone installing it by hand, and
# a missing library then looked exactly like a user who did not want an overlay.
# It is a native component of this tool, so it is built like one.

include(CheckCSourceCompiles)
include(CheckFunctionExists)
include(CheckIncludeFile)
include(CheckStructHasMember)
include(CheckSymbolExists)

set(CMAKE_REQUIRED_DEFINITIONS -D_GNU_SOURCE)

# Every one of these guards a wrapper for a function that may or may not exist
# on this libc. A wrapper compiled for a function that is absent would fail to
# link; one skipped for a function that is present is a hole through which a job
# reads the original tree or writes into it.
#
# Asked of the C library, never of its headers. What has to be redirected is not
# what this machine compiles, it is what other people's binaries call, and the
# two answers differ:
#
#   * glibc 2.33 stopped DECLARING the __xstat family and kept EXPORTING it
#     forever, so a header check says "absent" on any modern system while every
#     binary built before the change goes on calling it;
#   * the fortified entry points -- __open_2, __readlink_chk, __realpath_chk --
#     are never declared as ordinary functions at all. They are what a program
#     compiled with -D_FORTIFY_SOURCE=2 calls, which on Debian and Ubuntu is
#     every program in the image.
#
# MEASURED against the configuration this file actually produced: sixteen
# wrappers were being compiled out for functions the C library has, and the
# ninja in one project's build image imports __xstat64. On one path, in one
# process, at one instant:
#
#     stat()      -> ENOENT      <- python, clang, every probe written by hand
#     __xstat()   -> PRESENT     <- ninja
#     __xstat64() -> PRESENT
#
# so the build system saw an untouched tree, rebuilt nothing, and the test ran
# the previous candidate's binary. Two full runs ended with no verdict at all.
#
# check_function_exists links against the C library and needs no declaration, so
# it answers about the ABI. On a libc that genuinely lacks one -- musl has none
# of the legacy or fortified names -- it still says no and the wrapper stays out.
set(checked_symbols
    "canonicalize_file_name"
    "creat64"
    "eaccess"
    "euidaccess"
    "fchdir"
    "faccessat"
    "fchmodat"
    "fopen64"
    "freopen64"
    "futimesat"
    "futimens"
    "glob64"
    "linkat"
    "lstat64"
    "lutimes"
    "mkdirat"
    "open64"
    "openat"
    "openat64"
    "posix_spawn_file_actions_addopen"
    "readlinkat"
    "renameat"
    "scandir"
    "stat64"
    "statx"
    "symlinkat"
    "truncate64"
    "unlinkat"
    "utimensat"
    "vfork"
    "__open"
    "__open64"
    "__open_2"
    "__open64_2"
    "__openat_2"
    "__openat64_2"
    "__readlink_chk"
    "__readlinkat_chk"
    "__realpath_chk"
    "__chk_fail"
    "__xstat"
    "__xstat64"
    "__lxstat"
    "__lxstat64"
    "__fxstatat"
    "__fxstatat64"
    )
foreach(symbol ${checked_symbols})
  string(TOUPPER "HAVE_${symbol}" have_var)
  string(REGEX REPLACE "[^A-Z0-9_]" "_" have_var "${have_var}")
  check_function_exists("${symbol}" "${have_var}")
endforeach()

# A symbol checked here whose guard is not in config.h.in gets no #define, so
# its wrapper compiles out and the library quietly does not have it. That is the
# same silent omission this whole file has been chasing, arriving through the
# door of adding one. MEASURED the first time it happened: the wrapper was
# written, listed and compiled, and exported nothing.
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/config.h.in" config_template)
set(ungarded "")
foreach(symbol ${checked_symbols})
  string(TOUPPER "HAVE_${symbol}" have_var)
  string(REGEX REPLACE "[^A-Z0-9_]" "_" have_var "${have_var}")
  if(NOT config_template MATCHES "#cmakedefine ${have_var} ")
    list(APPEND ungarded "${have_var}")
  endif()
endforeach()
if(ungarded)
  message(FATAL_ERROR
    "config.h.in has no #cmakedefine for: ${ungarded}. Without it the wrapper is "
    "compiled out however present the function is, and nothing says so.")
endif()

check_include_file("alloca.h" HAVE_ALLOCA_H)
check_include_file("sys/syscall.h" HAVE_SYS_SYSCALL_H)
check_struct_has_member("struct dirent" d_namlen "dirent.h" HAVE_STRUCT_DIRENT_D_NAMLEN)

check_c_source_compiles(
  "__attribute__((visibility(\"hidden\"))) int f(void) { return 0; }
   int main(void) { return f(); }"
  HAVE___ATTRIBUTE__VISIBILITY)
check_c_source_compiles(
  "__attribute__((constructor)) static void f(void) { }
   int main(void) { return 0; }"
  HAVE___ATTRIBUTE__CONSTRUCTOR)
check_c_source_compiles(
  "static int x __attribute__((section(\"data.fakechroot\"))) = 0;
   int main(void) { return x; }"
  HAVE___ATTRIBUTE__SECTION_DATA_FAKECHROOT)

configure_file(config.h.in "${CMAKE_CURRENT_BINARY_DIR}/config.h")

file(GLOB overlay_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/src/*.c")

add_library(cvise_overlay SHARED ${overlay_SOURCES})
set_target_properties(cvise_overlay PROPERTIES
  # The reducer looks this name up in libexec; nothing about it is versioned,
  # because it is loaded by absolute path and never linked against.
  OUTPUT_NAME "cvise_overlay"
  PREFIX "lib"
  NO_SONAME ON
  # Emphatically NOT hidden: every wrapper here exists to be found by the
  # dynamic linker ahead of the libc symbol it shadows. Hiding them builds a
  # library that loads and interposes nothing, which is the one failure this
  # whole component is written to make impossible.
)
target_compile_definitions(cvise_overlay PRIVATE HAVE_CONFIG_H)
target_include_directories(cvise_overlay PRIVATE
  "${CMAKE_CURRENT_BINARY_DIR}"
  "${CMAKE_CURRENT_SOURCE_DIR}/src"
)
target_link_libraries(cvise_overlay PRIVATE ${CMAKE_DL_LIBS})

install(TARGETS cvise_overlay
  LIBRARY DESTINATION "${CMAKE_INSTALL_LIBEXECDIR}/${cvise_PACKAGE}"
)

# The probes. They are what makes the claims about this component checkable
# rather than merely stated: each drives a real process through the library and
# checks what that process actually saw -- that a deletion hides a file without
# touching the shared tree, that a compiler reads the variant through the
# original path, that a linker's output stays in the delta, that a job cannot
# damage what other jobs are reading.
find_package(Python3 COMPONENTS Interpreter REQUIRED)

file(GLOB overlay_TESTS CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/tests/*.py")
foreach(test_script IN LISTS overlay_TESTS)
  get_filename_component(test_name "${test_script}" NAME_WE)
  add_test(NAME "overlay.${test_name}"
    COMMAND "${Python3_EXECUTABLE}" "${test_script}")
  set_tests_properties("overlay.${test_name}" PROPERTIES
    ENVIRONMENT "CVISE_OVERLAY_LIB=$<TARGET_FILE:cvise_overlay>")
endforeach()
