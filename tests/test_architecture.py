"""Architectural fitness functions — they validate structure, not behaviour.

These do not exercise the domain. They assert that the rules the ADRs decided are
still true of the source, which is a different job from a unit test: the question
"does this need domain knowledge to run?" answers no for everything here, which is
what makes it a fitness function rather than a test.

The point is that the rules below erode silently. Nobody sets out to reintroduce a
bypass method or move query-building into the tool layer; it happens one convenient
edit at a time, and a code review a week later is too late because the habit has
already spread. Running as part of the build is what makes them stick.

Governs the Compliance sections of:
  docs/adr/0001-microkernel-with-single-opensearch-adapter.md
  docs/adr/0004-single-allowlist-as-the-read-only-enforcement-point.md
"""

import ast
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SERVER = REPO / "server.py"
CLIENT = REPO / "lib" / "client.py"


def _tree(path):
    return ast.parse(path.read_text())


def _functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]


def _has_decorator(fn, attr):
    return any(
        getattr(getattr(d, "func", d), "attr", None) == attr for d in fn.decorator_list
    )


# ── ADR 0004: one enforcement point, no bypass ────────────────────────────────


def test_no_method_issues_a_request_without_passing_check_path():
    """The read-only guarantee must be structural, not conventional.

    `raw_get`/`raw_post` used to skip `_check_path` by design, and both tools that
    accept a caller-supplied path went through them — so the allowlist never ran on
    the only paths an LLM can influence. Deleting them made `_get`/`_post` the only
    exits from the class. This asserts that remains true: every other place that
    touches `self._session.get/post` must be a known fixed-path caller.

    If this fails because you added a legitimate fixed-path call, add its function
    name to the allowlist below and say why in the commit. If it fails because you
    added a method taking a caller-supplied path, route it through `_get`/`_post`
    instead — that is the whole point.
    """
    permitted = {
        "_get",                  # calls _check_path first
        "_post",                 # calls _check_path first
        "_dashboards_proxy",     # reached only from _get/_post, path already checked
        "_resolve_backend",      # two literal probe URLs, no caller input
        "list_index_patterns",   # two literal Dashboards endpoints, no caller input
    }
    tree = _tree(CLIENT)
    offenders = {}
    for fn in _functions(tree):
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr in {"get", "post", "request"}
                and isinstance(f.value, ast.Attribute)
                and f.value.attr == "_session"
                and fn.name not in permitted
            ):
                offenders[fn.name] = offenders.get(fn.name, 0) + 1
    assert not offenders, (
        f"these functions issue HTTP directly without passing _check_path: {offenders}. "
        "Route them through _get/_post, or add them to `permitted` with a justification "
        "if the path is a fixed literal."
    )


@pytest.mark.parametrize("name", ["raw_get", "raw_post"])
def test_the_deleted_bypass_methods_have_not_come_back(name):
    """Named explicitly so a reintroduction fails loudly rather than being noticed
    later. A method whose docstring says "caller owns validation" is an invitation."""
    from lib.client import OpenSearchClient

    assert not hasattr(OpenSearchClient, name), (
        f"OpenSearchClient.{name} is back. See ADR 0004: a general-purpose "
        "any-path method re-opens the hole even if it happens to call _check_path "
        "today, because its name advertises a bypass to the next caller."
    )


def test_every_opensearch_api_docstring_example_passes_the_allowlist():
    """The tool docstring is the contract the model reads mid-incident.

    A docstring advertising a path the guard refuses costs an investigation cycle:
    the agent tries the documented example, gets a refusal, and has to guess. This
    keeps documentation and guard from drifting in either direction — extending the
    allowlist without documenting it, or documenting a path that is not allowed.
    """
    from lib.client import OpenSearchClient

    fn = next(
        f for f in _functions(_tree(SERVER)) if f.name == "opensearch_api"
    )
    doc = ast.get_docstring(fn) or ""
    examples = re.findall(r"^\s{4,}(/\S+)$", doc, re.M)
    assert examples, "no example paths found in the opensearch_api docstring"

    client = OpenSearchClient(opensearch_url="http://unused:9200")
    refused = []
    for path in examples:
        try:
            client._check_path("GET", path)
        except Exception as exc:  # any refusal at all is a failure here
            refused.append((path, type(exc).__name__))
    assert not refused, (
        f"opensearch_api documents paths its own guard refuses: {refused}. "
        "Either allowlist them or stop advertising them."
    )


# ── ADR 0001: the core stays a thin delegate ──────────────────────────────────


def test_server_does_not_import_an_http_library():
    """All OpenSearch knowledge belongs in the adapter (ADR 0001 rule 2). The core
    declares contracts and delegates; the moment it can speak HTTP, the boundary is
    gone and the tool layer becomes untestable without a network."""
    imported = set()
    for node in ast.walk(_tree(SERVER)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    banned = {"requests", "urllib", "urllib3", "http", "httpx", "socket"}
    assert not (imported & banned), (
        f"server.py imports {imported & banned}. HTTP belongs in lib/client.py."
    )


def test_server_contains_no_path_validation():
    """Path validation lived here once, in parallel with — and contradicting — the
    client's allowlist. One concern, one place: a second guard is not defence in
    depth when the two disagree about what is allowed.

    Checked against the AST, not the raw text, so a *comment* pointing the reader at
    `lib.client._ALLOWED_PATHS` is fine — that is useful signposting. What must not
    exist is executable validation.
    """
    tree = _tree(SERVER)

    assigned = {
        t.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    banned_names = {"_WRITE_PATH_FRAGMENTS", "_EXPLAIN_PATH_RE", "_ALLOWED_PATHS"}
    assert not (assigned & banned_names), (
        f"server.py defines {assigned & banned_names}. Read-only enforcement is the "
        "client's single responsibility — see ADR 0004."
    )

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for module in ("posixpath", "re"):
        assert module not in imported, (
            f"server.py imports {module}. Path normalisation and pattern matching on "
            "caller-supplied paths belong in the client, next to the allowlist."
        )


def test_every_tool_function_stays_a_thin_delegate():
    """A budget, not a style rule.

    `opensearch_compare` is the counter-example that motivated this: it accumulated
    set diffing, delta and percent-change arithmetic inside the tool function, which
    made it the one tool that cannot be tested without FastMCP. Computation grows in
    the core one convenient line at a time, so the ceiling is checked mechanically.

    A tool needing more than this is a signal the logic belongs in lib/, not a
    signal to raise the budget.
    """
    MAX_STATEMENTS = 8
    over = {}
    for fn in _functions(_tree(SERVER)):
        if not _has_decorator(fn, "tool"):
            continue
        body = [n for n in fn.body if not isinstance(n, ast.Expr)]  # skip docstring
        if len(body) > MAX_STATEMENTS:
            over[fn.name] = len(body)
    assert not over, (
        f"these tool functions exceed {MAX_STATEMENTS} statements: {over}. "
        "Move the computation into lib/ and keep the tool a declaration + delegation "
        "(ADR 0001 rule 1)."
    )


def test_the_tool_inventory_matches_the_module_docstring():
    """The module docstring lists every tool, so it is a second place the inventory
    lives and therefore a place it can drift. Cheap to check, so check it."""
    tree = _tree(SERVER)
    declared = {
        fn.name for fn in _functions(tree) if _has_decorator(fn, "tool")
    }
    doc = ast.get_docstring(tree) or ""
    listed = set(re.findall(r"^\s{2}(opensearch_\w+)", doc, re.M))
    assert declared == listed, (
        f"module docstring and @mcp.tool() set disagree — "
        f"only in code: {sorted(declared - listed)}; "
        f"only in docstring: {sorted(listed - declared)}"
    )
