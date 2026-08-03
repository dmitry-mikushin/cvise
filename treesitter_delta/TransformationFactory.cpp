#include "TransformationFactory.h"

#include <memory>
#include <string>

#include "EraseNamespace.h"
#include "ListDefinitions.h"
#include "RemoveFunction.h"
#include "ReplaceFunctionDefWithDecl.h"
#include "Transformation.h"

std::unique_ptr<Transformation> createTransformation(const std::string &Name) {
  if (Name == "replace-function-def-with-decl")
    return std::make_unique<FuncDefWithDeclReplacer>();
  if (Name == "erase-namespace")
    return std::make_unique<NamespaceEraser>();
  if (Name == "remove-function")
    return std::make_unique<FunctionRemover>();
  if (Name == "list-definitions")
    return std::make_unique<DefinitionLister>();
  return nullptr;
}
