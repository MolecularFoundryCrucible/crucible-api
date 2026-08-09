"""Every name a route handler calls must exist.

A refactor that renames or removes a helper can leave a call site behind. The
module still imports, the app still starts, and the route raises NameError the
first time someone hits it — which unit tests that import helpers directly will
never notice. This walks the AST of each route module and checks that every
global name a handler references is actually resolvable.

Written after a consolidation left two calls to a deleted `_require_manage_acl`
in resources.py, with a green suite.
"""
import ast
import builtins
import importlib
import pathlib

import pytest

ROUTE_FILES = sorted(pathlib.Path("app/api/routes").glob("*.py"))
BUILTINS = set(dir(builtins))


def _global_names_used(tree):
    """Names loaded at module scope or inside functions, minus locals."""
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
            for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
                bound.add(a.arg)
            if node.args.vararg:
                bound.add(node.args.vararg.arg)
            if node.args.kwarg:
                bound.add(node.args.kwarg.arg)
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    bound.add(n.id)
    return used - bound - BUILTINS


@pytest.mark.parametrize("path", ROUTE_FILES, ids=lambda p: p.name)
def test_every_referenced_name_resolves(path):
    tree = ast.parse(path.read_text())
    module = importlib.import_module(
        str(path.with_suffix("")).replace("/", "."))

    missing = sorted(
        n for n in _global_names_used(tree)
        if not hasattr(module, n) and n not in BUILTINS
    )
    assert not missing, (
        f"{path.name} references names that do not exist: {missing}. "
        f"A route calling one of these raises NameError at request time."
    )
