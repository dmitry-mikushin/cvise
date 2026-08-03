#ifndef LIST_DEFINITIONS_H
#define LIST_DEFINITIONS_H

#include <memory>
#include <optional>
#include <string>

#include <tree_sitter/api.h>

#include "Transformation.h"

// Emits the byte range of every top-level definition, and reduces nothing.
//
// The other transformations here answer "what could be removed". This one
// answers "where does each definition begin and end", which is what a caller
// needs in order to leave one of them alone: C-Vise refuses a candidate that
// altered a definition the source marked as not to be reduced, and to compare
// such a definition across two versions of a file it must first know how far
// it reaches. Braces cannot be counted for that -- braces inside strings,
// character literals, raw strings and comments are not braces, and the input
// is by construction half-destroyed source. The parser already knows.
//
// No marker name appears here. Which definition matters is the caller's
// business; this reports them all.
class DefinitionLister : public Transformation {
public:
  DefinitionLister();
  ~DefinitionLister() override;

  void processFile(const std::string &FileContents, TSTree &Tree,
                   std::optional<int> PathId) override;

private:
  std::unique_ptr<TSQuery, void (*)(TSQuery *)> Query;
};

#endif
