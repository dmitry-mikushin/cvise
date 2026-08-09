"""What counts as a C or C++ source file.

Its own module, and that needs a reason. Two places ask the question: the clang
pass, which decides what it will try to transform, and the reduction guard,
which decides what can carry a CVISE_NOREDUCE marker. They must agree -- a file
the passes will rewrite and a file the guard will protect have to be the same
set, or the guard protects something nothing was going to touch and misses
something that is.

They cannot share it through either of their own modules. MEASURED: importing
cvise.passes.clang costs 64 ms, and the guard is imported by
cvise/utils/projectcheck.py, which runs once per candidate -- over a hundred
thousand candidates that is two hours of interpreter startup to learn a tuple
of 23 strings. This module imports nothing.
"""

SOURCE_SUFFIXES = (
    '.c', '.cc', '.cp', '.cpp', '.cxx', '.c++', '.C', '.m', '.mm', '.cl', '.cu', '.hip',
    '.h', '.hh', '.hp', '.hpp', '.hxx', '.h++', '.H', '.inc', '.ipp', '.tcc', '.tpp',
)
