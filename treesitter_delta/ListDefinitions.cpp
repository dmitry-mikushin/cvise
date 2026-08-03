#include "ListDefinitions.h"

#include "Parsers.h"
#include "TreeSitterUtils.h"

#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <iterator>
#include <string>

#include <tree_sitter/api.h>

// Every function definition, at any nesting depth. Nothing is captured but the
// node itself: the caller decides which one it cares about by looking at the
// text, so a name capture here would only be a second, narrower answer to a
// question already answered by the range.
constexpr char QueryStr[] = R"(
  (function_definition) @definition
)";

DefinitionLister::DefinitionLister() : Query(nullptr, ts_query_delete) {
  uint32_t ErrorOffset = 0;
  TSQueryError ErrorType = TSQueryErrorNone;
  Query.reset(ts_query_new(tree_sitter_cpp(), QueryStr, std::size(QueryStr) - 1,
                           &ErrorOffset, &ErrorType));
  if (!Query) {
    std::cerr << "Failed to init Tree-sitter query: error " << ErrorType
              << " offset " << ErrorOffset << "\n";
    std::exit(-1);
  }
}

DefinitionLister::~DefinitionLister() = default;

void DefinitionLister::processFile(const std::string & /*FileContents*/,
                                   TSTree &Tree, std::optional<int> PathId) {
  std::unique_ptr<TSQueryCursor, decltype(&ts_query_cursor_delete)> Cursor(
      ts_query_cursor_new(), ts_query_cursor_delete);
  ts_query_cursor_exec(Cursor.get(), Query.get(), ts_tree_root_node(&Tree));

  TSQueryMatch Match;
  while (ts_query_cursor_next_match(Cursor.get(), &Match)) {
    for (int I = 0; I < Match.capture_count; ++I) {
      const TSNode &N = Match.captures[I].node;
      // A definition preceded by "template <" starts at the template, the same
      // way removal treats it: the marker a caller is looking for may well sit
      // above the template head.
      TSNode Template = walkUpNodeWithType(N, "template_declaration");
      TSNode Whole = ts_node_is_null(Template) ? N : Template;
      std::cout << "{\"l\":" << ts_node_start_byte(Whole)
                << ",\"r\":" << ts_node_end_byte(Whole);
      if (PathId.has_value())
        std::cout << ",\"p\":" << *PathId;
      std::cout << "}\n";
    }
  }
}
