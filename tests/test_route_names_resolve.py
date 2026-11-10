"""Every name a route handler uses must actually be defined there.

A refactor that renames a parameter or drops a setup line can leave a call
site behind. The module still imports, the app still starts, and the route
raises NameError the first time someone hits it — which unit tests that
import helpers directly never notice.

Written after a consolidation left calls to a deleted `_require_manage_acl`
in resources.py, with a green suite. A first version of this file walked the
AST collecting module-wide bound names, which is too coarse: it treats a name
bound as a parameter of *one* function as defined everywhere in the module.
That version passed while `_sample_base_query(principal)` used `auth_res` in
its body and every `GET /samples` call was a NameError. pyflakes does the
scope analysis properly, so use it rather than re-deriving it here.
"""
import pathlib

import pytest

pyflakes_api = pytest.importorskip(
    "pyflakes.api", reason="pyflakes is a dev dependency; see pyproject.toml")
from pyflakes.reporter import Reporter  # noqa: E402

CHECKED_DIRS = ["app/api/routes", "app/utils", "app/models", "app/auth", "app/services"]
FILES = sorted([*(p for d in CHECKED_DIRS for p in pathlib.Path(d).glob("*.py")), pathlib.Path("app/tables.py")])

# Unused imports and star-import noise are style, not breakage. Only names
# that do not resolve, and syntax errors, can fail a request at runtime.
FATAL = ("undefined name", "invalid syntax", "syntax error")


class _Collect:
    def __init__(self):
        self.messages = []

    def unexpectedError(self, filename, msg):
        self.messages.append(f"{filename}: {msg}")

    def syntaxError(self, filename, msg, lineno, offset, text):
        self.messages.append(f"{filename}:{lineno}: {msg}")

    def flake(self, message):
        self.messages.append(str(message))


@pytest.mark.parametrize("path", FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_undefined_names(path):
    reporter = _Collect()
    pyflakes_api.checkPath(str(path), reporter)

    fatal = [
        m for m in reporter.messages
        if any(f in m.lower() for f in FATAL)
    ]
    assert not fatal, (
        f"{path} uses names that are not defined in scope:\n  "
        + "\n  ".join(fatal)
        + "\nA route hitting one of these raises NameError at request time."
    )
