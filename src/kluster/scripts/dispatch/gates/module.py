"""A Python module's top-level bindings, and how much of each one an item's context gives.

A test module's and the program's are read alike: an assignment or an
import is its whole statement, since its value is what an assertion may
restate; a function or a class is its signature and docstring, since a case
reads it by what it returns or makes. The one exception is a test module's
helper that a case calls, which the change-detector extractor gives whole
(`change_detector.assertions`), since what the case asserts on is what its
body computes.
"""

from __future__ import annotations

import ast

Function = ast.FunctionDef | ast.AsyncFunctionDef
Binding = ast.Assign | ast.AnnAssign | ast.AugAssign | ast.Import | ast.ImportFrom | Function | ast.ClassDef


def bindings(tree: ast.Module) -> dict[str, Binding]:
    """Each name the module binds at its top level, and the statement binding it last."""
    bound: dict[str, Binding] = {}
    for statement in tree.body:
        match statement:
            case ast.Import() | ast.ImportFrom():
                for alias in statement.names:
                    bound[(alias.asname or alias.name).split('.')[0]] = statement
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
                bound[statement.name] = statement
            case ast.Assign():
                for target in statement.targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name):
                            bound[name.id] = statement
            case ast.AnnAssign(target=ast.Name(id=name)) | ast.AugAssign(target=ast.Name(id=name)):
                bound[name] = statement
            case _:
                pass
    return bound


def first_line(statement: Binding) -> int:
    """Where a statement opens: its first decorator, where it has one."""
    return min([statement.lineno, *(d.lineno for d in getattr(statement, 'decorator_list', ()))])


def definition_end(statement: Binding) -> int:
    """The last line of what is given of a statement: all of it, or a definition's signature and docstring."""
    if not isinstance(statement, Function | ast.ClassDef):
        return statement.end_lineno or statement.lineno
    match statement.body[0]:
        case ast.Expr(value=ast.Constant(value=str())) as docstring:
            return docstring.end_lineno or docstring.lineno
        case opening:
            return max(statement.lineno, opening.lineno - 1)
