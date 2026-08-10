"""Fitness functions for values that must agree across files.

Governs the Compliance section of docs/adr/0003-single-source-of-truth-for-version.md.

These are the "important but not urgent" checks that erode silently. Nothing breaks
today when the Makefile's version drifts from pyproject.toml's — it breaks later, for
someone downstream, in a way that is hard to trace back. That already happened here:
the Makefile said 0.3.3 while pyproject said 0.4.0, so `make push` published an image
tagged 0.3.3 containing 0.4.0 code and moved `latest` onto it.

Deriving a value is always better than checking two copies agree. Where derivation is
practical it has been done — the Makefile now reads pyproject, and the User-Agent
comes from package metadata. These checks cover what is left, and fail the build if a
literal reappears.
"""

import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"
MAKEFILE = REPO / "Makefile"
REQUIREMENTS = REPO / "requirements.txt"

if sys.version_info >= (3, 11):
    import tomllib
else:  # the project floor is 3.10; tomllib arrived in 3.11
    tomllib = None


def _pyproject() -> dict:
    if tomllib is None:
        pytest.skip("tomllib requires Python 3.11+; covered on the rest of the matrix")
    return tomllib.loads(PYPROJECT.read_text())


def _declared_version() -> str:
    """Read the version the same crude way the Makefile does, so this check also
    validates that the Makefile's grep still finds it."""
    m = re.search(r'^version = "([^"]+)"', PYPROJECT.read_text(), re.M)
    assert m, "no top-level `version = \"...\"` line in pyproject.toml"
    return m.group(1)


# ── One authoritative version ─────────────────────────────────────────────────


def test_no_version_literal_outside_pyproject():
    """A hardcoded version anywhere else is drift waiting to happen.

    The specific instance this replaces: `lib/client.py` carried
    `"User-Agent": "mcp-opensearch/0.4.0"`. A User-Agent is what a cluster operator
    sees in their logs when this client misbehaves, so it must not lie about the
    version — and the only way to guarantee that is to not write it down twice.
    """
    version = _declared_version()
    offenders = []
    for path in [REPO / "server.py", REPO / "lib" / "client.py", REPO / "lib" / "compare.py"]:
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if f"mcp-opensearch/{version}" in line or re.search(
                rf'["\']{re.escape(version)}["\']', line
            ):
                offenders.append(f"{path.relative_to(REPO)}:{i}: {line.strip()}")
    assert not offenders, (
        "version literals found outside pyproject.toml:\n  "
        + "\n  ".join(offenders)
        + "\nDerive it instead — see lib/client.py::_package_version()."
    )


def test_makefile_derives_the_version_and_does_not_declare_it():
    """`make push` tags the published image, so a stale literal here mislabels a
    release. The Makefile must read pyproject rather than restate it."""
    src = MAKEFILE.read_text()
    hardcoded = re.search(r"^VERSION\s*:?=\s*[0-9]", src, re.M)
    assert not hardcoded, (
        "Makefile declares VERSION as a literal. It must derive from pyproject.toml — "
        "it drifted to 0.3.3 against a 0.4.0 pyproject and mislabelled a pushed image."
    )
    assert "pyproject.toml" in src, "Makefile no longer reads the version from pyproject.toml"


def test_makefile_fails_closed_when_the_version_cannot_be_read():
    """The derivation parses TOML with grep/cut, which is fragile if the `version`
    line ever changes shape. That is acceptable only because the failure is a loud
    stop rather than a push tagged with an empty string."""
    assert re.search(r"test -n .\$\(VERSION\)", MAKEFILE.read_text()), (
        "the `push` target no longer guards against an empty VERSION"
    )


def test_user_agent_reports_the_real_version_when_installed():
    """Round-trip the derivation. In a source checkout there is no distribution
    metadata, so "unknown" is the correct and documented answer — what must never
    happen is a stale hardcoded number, or an exception at import."""
    from lib.client import _package_version

    value = _package_version()
    assert isinstance(value, str) and value
    if value != "unknown":
        assert value == _declared_version(), (
            f"installed distribution reports {value} but pyproject declares "
            f"{_declared_version()}"
        )


# ── Dependencies declared twice ───────────────────────────────────────────────


def test_requirements_txt_and_pyproject_dependencies_agree():
    """Two files declare the runtime dependencies and both are load-bearing:
    `requirements.txt` is what the Dockerfile installs, `pyproject.toml` is what the
    PyPI wheel declares. If they diverge, the image and the package ship different
    dependency sets and only one of them gets tested by CI.
    """
    declared = _pyproject()["project"]["dependencies"]
    listed = [
        line.strip()
        for line in REQUIREMENTS.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert sorted(listed) == sorted(declared), (
        "requirements.txt and pyproject.toml [project.dependencies] disagree.\n"
        f"  only in requirements.txt: {sorted(set(listed) - set(declared))}\n"
        f"  only in pyproject.toml:   {sorted(set(declared) - set(listed))}\n"
        "The Dockerfile installs the former and the wheel declares the latter."
    )


# ── Declared support must be tested support ───────────────────────────────────


def test_ci_matrix_covers_the_declared_python_floor():
    """`requires-python` and the trove classifiers are a promise to users. An
    untested floor is an unverified promise, so the CI matrix must include it."""
    project = _pyproject()["project"]
    floor = re.search(r">=\s*(\d+\.\d+)", project["requires-python"])
    assert floor, f"cannot parse requires-python: {project['requires-python']!r}"

    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert f'"{floor.group(1)}"' in ci, (
        f"pyproject declares Python >= {floor.group(1)} but the CI matrix does not "
        "test it. Either test the floor or raise it."
    )


def test_classifiers_do_not_claim_untested_python_versions():
    """A `Programming Language :: Python :: 3.x` classifier is a support claim that
    package indexes surface to users; it should not outrun the test matrix."""
    project = _pyproject()["project"]
    claimed = {
        c.rsplit("::", 1)[-1].strip()
        for c in project.get("classifiers", [])
        if c.startswith("Programming Language :: Python :: 3.")
    }
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    untested = {v for v in claimed if f'"{v}"' not in ci}
    assert not untested, (
        f"these Python versions are claimed in classifiers but absent from the CI "
        f"matrix: {sorted(untested)}"
    )
