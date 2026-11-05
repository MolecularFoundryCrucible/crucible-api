from html.parser import HTMLParser
from urllib.parse import quote

import pytest

from app.api.routes.auth import _email_prompt_html


class PromptParser(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.elements = []
        self.scripts = []
        self.in_script = False
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        if tag == "script":
            self.in_script = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.scripts.append(data)

    def by_id(self, element_id):
        return next(attrs for _, attrs in self.elements if attrs.get("id") == element_id)


@pytest.mark.parametrize("field", ["first_name", "last_name", "orcid", "unique_id", "crucible_token", "root"])
@pytest.mark.parametrize("value", [
    'O\'Connor & "Co" <team>',
    '\"><img src=x onerror="alert(1)">',
    "</script><script>alert(1)</script>",
    "';alert(1);//\\\n\u2028",
    "François 李 &quot;",
])
def test_prompt_values_remain_data(field, value):
    args = dict(root="/api/v3", orcid="0000-0000-0000-0001", unique_id="0000-0000-0000-0001",
                crucible_token="synthetic-key", first_name="Alice", last_name="Smith")
    baseline = PromptParser(_email_prompt_html(**args))
    args[field] = value
    parsed = PromptParser(_email_prompt_html(**args))

    assert [tag for tag, _ in parsed.elements] == [tag for tag, _ in baseline.elements]
    assert [set(attrs) for _, attrs in parsed.elements] == [set(attrs) for _, attrs in baseline.elements]
    assert parsed.scripts == baseline.scripts
    for name in ("first_name", "last_name", "orcid"):
        assert parsed.by_id(name)["value"] == args[name]
    form = parsed.by_id("form-view")
    assert form["data-api-key"] == args["crucible_token"]
    assert form["data-update-url"] == f"{quote(args['root'], safe='/')}/users/{quote(args['unique_id'], safe='')}"


def test_prompt_keeps_existing_submission_contract():
    parsed = PromptParser(_email_prompt_html("/api/v3", "0000-0000-0000-0001", "0000-0000-0000-0001", "synthetic-key"))
    assert parsed.by_id("first_name")["value"] == ""
    assert parsed.by_id("last_name")["value"] == ""
    assert parsed.by_id("form-view")["data-update-url"] == "/api/v3/users/0000-0000-0000-0001"
    script = "".join(parsed.scripts)
    assert "fetch(form.dataset.updateUrl" in script
    assert "'Authorization': 'Bearer ' + form.dataset.apiKey" in script
    assert "textContent = form.dataset.apiKey" in script
    assert "method: 'PATCH'" in script
    assert "JSON.stringify({email: email})" in script
