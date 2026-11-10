import ast
import pathlib

BLOCKING = {"Session", "load_login_config", "get_secret", "optional_secret", "sleep"}


def _direct_calls(node):
    """Calls an async function makes itself, not those inside a nested function handed to the threadpool."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(child, ast.Call):
            yield child
        yield from _direct_calls(child)


def test_async_functions_leave_database_and_secret_reads_to_the_threadpool():
    found = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.AsyncFunctionDef):
                for call in _direct_calls(node):
                    name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                    if name in BLOCKING:
                        found.append(f"{path}:{call.lineno} {node.name} calls {name}()")
    assert found == [], "These run on the event loop and stall every request on the worker; use run_in_threadpool"
