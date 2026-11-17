import ast
import pathlib

BLOCKING = {"Session", "load_login_config", "get_secret", "optional_secret", "sleep"}
STARTUP = {("app/api/main.py", "lifespan")}


def _direct_calls(node):
    """Calls a function makes itself, not those inside a nested function handed to the threadpool."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(child, ast.Call):
            yield child
        yield from _direct_calls(child)


class _Modules:
    """Module-level functions of app/, and what each module imports from app/ by name."""

    def __init__(self):
        self.functions, self.imports = {}, {}
        for path in sorted(pathlib.Path("app").rglob("*.py")):
            module = ".".join(path.with_suffix("").parts).removesuffix(".__init__")
            tree = ast.parse(path.read_text())
            self.functions[module] = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
            self.imports[module] = {
                alias.asname or alias.name: (node.module, alias.name)
                for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app")
                for alias in node.names
            }

    def resolve(self, module, name):
        if name in self.functions.get(module, {}):
            return module, self.functions[module][name]
        if name in self.imports.get(module, {}):
            target, original = self.imports[module][name]
            if original in self.functions.get(target, {}):
                return target, self.functions[target][original]
        return None

    def blocking_path(self, module, name, seen=frozenset()):
        """The chain of helpers through which calling `name` from `module` reaches a blocking read."""
        if name in BLOCKING:
            return [name]
        found = self.resolve(module, name)
        if found is None or (found[0], name) in seen:
            return None
        target, node = found
        for call in _direct_calls(node):
            path = self.blocking_path(target, getattr(call.func, "id", None) or getattr(call.func, "attr", None), seen | {(target, name)})
            if path:
                return [name, *path]
        return None


def test_async_functions_leave_database_and_secret_reads_to_the_threadpool():
    modules, found = _Modules(), []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        module = ".".join(path.with_suffix("").parts).removesuffix(".__init__")
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.AsyncFunctionDef) and (str(path), node.name) not in STARTUP:
                for call in _direct_calls(node):
                    chain = modules.blocking_path(module, getattr(call.func, "id", None) or getattr(call.func, "attr", None))
                    if chain:
                        found.append(f"{path}:{call.lineno} {node.name} calls {' -> '.join(chain)}()")
    assert found == [], "These run on the event loop and stall every request on the worker; use run_in_threadpool:\n" + "\n".join(found)
