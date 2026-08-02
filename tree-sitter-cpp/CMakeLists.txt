cmake_minimum_required(VERSION 3.13)

project(tree-sitter-cpp
        VERSION "0.23.4"
        DESCRIPTION "C++ grammar for tree-sitter"
        HOMEPAGE_URL "https://github.com/tree-sitter/tree-sitter-cpp"
        LANGUAGES C)

option(BUILD_SHARED_LIBS "Build using shared libraries" ON)
option(TREE_SITTER_REUSE_ALLOCATOR "Reuse the library allocator" OFF)

set(TREE_SITTER_ABI_VERSION 14 CACHE STRING "Tree-sitter ABI version")
if(NOT ${TREE_SITTER_ABI_VERSION} MATCHES "^[0-9]+$")
    unset(TREE_SITTER_ABI_VERSION CACHE)
    message(FATAL_ERROR "TREE_SITTER_ABI_VERSION must be an integer")
endif()

# src/parser.c is a source file here, not a build artifact.
#
# Upstream's CMakeLists regenerates it from src/grammar.json with the
# tree-sitter CLI, writing the result back into the source tree. That is right
# for someone developing the grammar and wrong for everyone vendoring it, in two
# ways at once. Without the CLI, `find_program` leaves the literal string
# TREE_SITTER_CLI-NOTFOUND and the rule runs it: a clean build of C-Vise stopped
# with "TREE_SITTER_CLI-NOTFOUND: command not found", on a file that has been
# committed all along. With the CLI it is worse -- a fresh clone gives
# grammar.json and parser.c the same timestamp, so the rule fires and silently
# rewrites 23 MB of tracked source before anyone has changed anything.
#
# Everything generated is committed: parser.c, scanner.c, node-types.json and
# grammar.json, beside the grammar.js they come from. Updating the vendored
# grammar is therefore a deliberate act, done once and reviewed like any other
# change:
#
#     cd tree-sitter-cpp && tree-sitter generate src/grammar.json --abi=14
#
# and not something a build does behind the back of whoever ran it.
if(NOT EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/parser.c")
    message(FATAL_ERROR
            "tree-sitter-cpp/src/parser.c is missing. It is committed to this repository, so "
            "this checkout is incomplete; regenerate it with "
            "`tree-sitter generate src/grammar.json --abi=${TREE_SITTER_ABI_VERSION}` "
            "in ${CMAKE_CURRENT_SOURCE_DIR}, or restore it from git.")
endif()

add_library(tree-sitter-cpp src/parser.c)
if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/src/scanner.c)
  target_sources(tree-sitter-cpp PRIVATE src/scanner.c)
endif()
target_include_directories(tree-sitter-cpp PRIVATE src)

target_compile_definitions(tree-sitter-cpp PRIVATE
                           $<$<BOOL:${TREE_SITTER_REUSE_ALLOCATOR}>:TREE_SITTER_REUSE_ALLOCATOR>
                           $<$<CONFIG:Debug>:TREE_SITTER_DEBUG>)

set_target_properties(tree-sitter-cpp
                      PROPERTIES
                      C_STANDARD 11
                      POSITION_INDEPENDENT_CODE ON
                      SOVERSION "${TREE_SITTER_ABI_VERSION}.${PROJECT_VERSION_MAJOR}"
                      DEFINE_SYMBOL "")

configure_file(bindings/c/tree-sitter-cpp.pc.in
               "${CMAKE_CURRENT_BINARY_DIR}/tree-sitter-cpp.pc" @ONLY)

include(GNUInstallDirs)

install(FILES bindings/c/tree-sitter-cpp.h
        DESTINATION "${CMAKE_INSTALL_INCLUDEDIR}/tree_sitter")
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/tree-sitter-cpp.pc"
        DESTINATION "${CMAKE_INSTALL_DATAROOTDIR}/pkgconfig")
install(TARGETS tree-sitter-cpp
        LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}")
