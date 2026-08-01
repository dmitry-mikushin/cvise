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
include(CheckIncludeFile)
include(CheckStructHasMember)
include(CheckSymbolExists)

set(CMAKE_REQUIRED_DEFINITIONS -D_GNU_SOURCE)

# Every one of these guards a wrapper for a function that may or may not exist
# on this libc. A wrapper compiled for a function that is absent would fail to
# link; one skipped for a function that is present is a hole through which a job
# writes into the shared tree.
foreach(symbol_and_header
    "canonicalize_file_name stdlib.h"
    "creat64 fcntl.h"
    "eaccess unistd.h"
    "euidaccess unistd.h"
    "fchdir unistd.h"
    "fchmodat sys/stat.h"
    "fopen64 stdio.h"
    "freopen64 stdio.h"
    "futimesat sys/time.h"
    "glob64 glob.h"
    "linkat unistd.h"
    "lstat64 sys/stat.h"
    "lutimes sys/time.h"
    "mkdirat sys/stat.h"
    "mkdtemp stdlib.h"
    "mkostemp stdlib.h"
    "mkstemps stdlib.h"
    "open64 fcntl.h"
    "openat fcntl.h"
    "openat64 fcntl.h"
    "readlinkat unistd.h"
    "renameat stdio.h"
    "scandir dirent.h"
    "stat64 sys/stat.h"
    "statx sys/stat.h"
    "symlinkat unistd.h"
    "truncate64 unistd.h"
    "unlinkat unistd.h"
    "utimensat sys/stat.h"
    "vfork unistd.h"
    "__fxstatat sys/stat.h"
    "__fxstatat64 sys/stat.h"
    "__lxstat sys/stat.h"
    "__lxstat64 sys/stat.h"
    "__xstat sys/stat.h"
    "__xstat64 sys/stat.h"
    "__open fcntl.h"
    "__open64 fcntl.h"
    "__open_2 fcntl.h"
    "__open64_2 fcntl.h"
    "__openat_2 fcntl.h"
    "__openat64_2 fcntl.h"
    "__readlink_chk unistd.h"
    "__readlinkat_chk unistd.h"
    "__realpath_chk stdlib.h"
    "__chk_fail stdlib.h"
    )
  separate_arguments(parts UNIX_COMMAND "${symbol_and_header}")
  list(GET parts 0 symbol)
  list(GET parts 1 header)
  string(TOUPPER "HAVE_${symbol}" have_var)
  string(REGEX REPLACE "[^A-Z0-9_]" "_" have_var "${have_var}")
  check_symbol_exists("${symbol}" "${header}" "${have_var}")
endforeach()

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
