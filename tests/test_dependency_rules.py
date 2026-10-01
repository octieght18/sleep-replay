"""Dependency_Rules test (Requirement 17.2, 17.3).

Walks every ``.py`` file under ``backend/``, parses it with :mod:`ast`, and
resolves every ``import`` / ``from ... import`` statement (absolute or
relative, module-level or nested inside functions, classes, ``if``/``try``
blocks, including ``TYPE_CHECKING`` blocks) to a Backend package or Importer
sub-package. Each resolved dependency is checked against the rules:

(a) ``domain`` imports no other Backend package.
(b) ``ingestion`` imports no ``processing``, ``sonification``, ``audio``, or ``api`` package.
(c) no Importer sub-package imports another Importer sub-package.
(d) ``processing``, ``sonification``, and ``audio`` import no ``ingestion`` package.
(e) ``ingestion``, ``domain``, ``processing``, ``sonification``, and ``audio`` import no ``api`` package.
(p) ``persistence`` imports no Backend package other than ``domain``.

Standard-library and third-party imports are unrestricted. A failure names the
violating source file (with line number), the imported module, and the Backend
package it resolves to.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = REPO_ROOT / "backend"
BACKEND_NAME = "backend"

# Top-level Backend packages (Requirement 17.1 plus the persistence foundation).
BACKEND_PACKAGES = frozenset(
    {"domain", "ingestion", "processing", "persistence", "sonification", "audio", "api"}
)
# Importer sub-packages of ``ingestion`` (rule (c)).
IMPORTER_SUBPACKAGES = frozenset({"fitbit", "sensorpush"})

RULE_TEXT = {
    "a": "the domain package imports no other Backend package",
    "b": "the ingestion package imports no processing, sonification, audio, or api package",
    "c": "no Importer sub-package imports another Importer sub-package",
    "d": "the processing, sonification, and audio packages import no ingestion package",
    "e": "the ingestion, domain, processing, sonification, and audio packages import no api package",
    "p": "the persistence package imports only the domain package",
}


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ImportRef:
    """One imported module name found in a source file."""

    lineno: int
    module: str  # fully-qualified dotted name, e.g. "backend.ingestion.fitbit.importer"


@dataclass(frozen=True)
class Violation:
    source_file: str  # path relative to the repository root, forward slashes
    lineno: int
    imported_module: str
    imported_unit: str  # Backend package or Importer sub-package, e.g. "ingestion.fitbit"
    rule: str

    def describe(self) -> str:
        return (
            f"{self.source_file}:{self.lineno} imports '{self.imported_module}' "
            f"(Backend package '{self.imported_unit}') - violates rule ({self.rule}): "
            f"{RULE_TEXT[self.rule]}"
        )


def module_name_for(path: Path, backend_root: Path) -> tuple[str, bool]:
    """Return (dotted module name, is_package) for a file under ``backend_root``."""
    rel = path.relative_to(backend_root).with_suffix("")
    parts = [backend_root.name, *rel.parts]
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def unit_of(module: str) -> str | None:
    """Resolve a dotted module name to a Backend package / Importer sub-package.

    Returns e.g. ``"domain"``, ``"ingestion"``, ``"ingestion.fitbit"``, or
    ``None`` when the module is not inside a Backend package (stdlib,
    third-party, or the ``backend`` root itself).

    Bare top-level names that match a Backend package (``import domain``) are
    also treated as Backend imports, so a changed ``sys.path`` cannot be used
    to bypass the rules.
    """
    parts = module.split(".")
    if parts[0] == BACKEND_NAME:
        parts = parts[1:]
    elif parts[0] not in BACKEND_PACKAGES:
        return None
    if not parts or parts[0] not in BACKEND_PACKAGES:
        return None
    if parts[0] == "ingestion" and len(parts) > 1 and parts[1] in IMPORTER_SUBPACKAGES:
        return f"ingestion.{parts[1]}"
    return parts[0]


def iter_imports(tree: ast.AST, module: str, is_package: bool) -> Iterator[ImportRef]:
    """Yield every module imported anywhere in ``tree`` as a fully-qualified name."""
    current_package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield ImportRef(node.lineno, alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = current_package.split(".") if current_package else []
                drop = node.level - 1
                if drop > len(base_parts) - 1:
                    # Relative import escaping the top-level package: Python
                    # rejects it at runtime; report it against the backend root.
                    base_parts = base_parts[:1]
                elif drop:
                    base_parts = base_parts[:-drop]
                base = ".".join(base_parts)
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    yield ImportRef(node.lineno, target)
                    continue
                # ``from pkg import name`` may import a sub-module ``pkg.name``.
                # Prefer that resolution when it names a Backend package or
                # Importer sub-package; otherwise the dependency is on ``pkg``.
                candidate = f"{target}.{alias.name}"
                if unit_of(candidate) is not None and unit_of(candidate) != unit_of(target):
                    yield ImportRef(node.lineno, candidate)
                else:
                    yield ImportRef(node.lineno, target)


def rules_violated(owner: str, target: str) -> list[str]:
    """Return the rule labels violated when unit ``owner`` imports unit ``target``."""
    owner_pkg = owner.split(".")[0]
    target_pkg = target.split(".")[0]
    violated: list[str] = []
    if owner_pkg == "domain" and target_pkg != "domain":
        violated.append("a")
    if owner_pkg == "ingestion" and target_pkg in {"processing", "sonification", "audio", "api"}:
        violated.append("b")
    if (
        owner.startswith("ingestion.")
        and target.startswith("ingestion.")
        and owner != target
    ):
        violated.append("c")
    if owner_pkg in {"processing", "sonification", "audio"} and target_pkg == "ingestion":
        violated.append("d")
    if (
        owner_pkg in {"ingestion", "domain", "processing", "sonification", "audio"}
        and target_pkg == "api"
    ):
        violated.append("e")
    if owner_pkg == "persistence" and target_pkg not in {"persistence", "domain"}:
        violated.append("p")
    return violated


def backend_source_files(backend_root: Path) -> list[Path]:
    return sorted(
        p for p in backend_root.rglob("*.py") if "__pycache__" not in p.parts
    )


def find_violations(backend_root: Path) -> list[Violation]:
    """Check every source file under ``backend_root`` against the Dependency_Rules."""
    repo_root = backend_root.parent
    violations: list[Violation] = []
    for path in backend_source_files(backend_root):
        module, is_package = module_name_for(path, backend_root)
        owner = unit_of(module)
        if owner is None:
            continue  # the ``backend`` root __init__ belongs to no Backend package
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        rel = path.relative_to(repo_root).as_posix()
        for ref in iter_imports(tree, module, is_package):
            target = unit_of(ref.module)
            if target is None:
                continue
            for rule in rules_violated(owner, target):
                violations.append(Violation(rel, ref.lineno, ref.module, target, rule))
    return violations


# --------------------------------------------------------------------------- #
# The real check against the repository (Requirement 17.2, 17.3)
# --------------------------------------------------------------------------- #


def test_backend_source_files_found() -> None:
    files = backend_source_files(BACKEND_ROOT)
    owners = {unit_of(module_name_for(p, BACKEND_ROOT)[0]) for p in files}
    assert BACKEND_PACKAGES <= {o.split(".")[0] for o in owners if o}
    assert {f"ingestion.{s}" for s in IMPORTER_SUBPACKAGES} <= owners


def test_backend_satisfies_dependency_rules() -> None:
    violations = find_violations(BACKEND_ROOT)
    if violations:
        pytest.fail(
            "Dependency_Rules violated:\n" + "\n".join(v.describe() for v in violations),
            pytrace=False,
        )


# --------------------------------------------------------------------------- #
# Self-tests of the resolver against synthetic Backend trees
# --------------------------------------------------------------------------- #


def _make_backend(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "backend"
    skeleton = [
        "__init__.py",
        *(f"{pkg}/__init__.py" for pkg in BACKEND_PACKAGES),
        *(f"ingestion/{sub}/__init__.py" for sub in IMPORTER_SUBPACKAGES),
    ]
    for rel in skeleton:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).touch()
    for rel, source in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(source, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("backend.domain.telemetry", "domain"),
        ("backend.ingestion", "ingestion"),
        ("backend.ingestion.registry", "ingestion"),
        ("backend.ingestion.fitbit.importer", "ingestion.fitbit"),
        ("backend.ingestion.sensorpush", "ingestion.sensorpush"),
        ("domain.units", "domain"),
        ("backend", None),
        ("os.path", None),
        ("numpy", None),
        ("backend.nonexistent", None),
    ],
)
def test_unit_of(unit: str, expected: str | None) -> None:
    assert unit_of(unit) == expected


@pytest.mark.parametrize(
    ("rel_path", "source", "rule", "imported_unit"),
    [
        # (a) domain imports another Backend package (absolute, module level)
        ("domain/session.py", "from backend.processing import aligner\n", "a", "processing"),
        # (a) domain -> persistence via a relative import
        ("domain/errors.py", "from ..persistence import data_directory\n", "a", "persistence"),
        # (b) ingestion -> processing inside a function
        (
            "ingestion/registry.py",
            "def f():\n    import backend.processing.aligner\n",
            "b",
            "processing",
        ),
        # (c) fitbit -> sensorpush via a relative import
        ("ingestion/fitbit/importer.py", "from ...ingestion.sensorpush import units\n", "c", "ingestion.sensorpush"),
        # (c) sensorpush -> fitbit via ``from pkg import subpackage``
        ("ingestion/sensorpush/importer.py", "from .. import fitbit\n", "c", "ingestion.fitbit"),
        # (d) sonification -> ingestion inside a class method
        (
            "sonification/mapper.py",
            "class M:\n    def m(self):\n        from backend.ingestion import registry\n",
            "d",
            "ingestion",
        ),
        # (e) processing -> api
        ("processing/events.py", "import backend.api.pipeline as p\n", "e", "api"),
        # (e) audio -> api inside a TYPE_CHECKING block
        (
            "audio/render.py",
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from backend.api import pipeline\n",
            "e",
            "api",
        ),
        # persistence imports something other than domain
        ("persistence/telemetry_store.py", "from backend.ingestion import registry\n", "p", "ingestion"),
    ],
)
def test_resolver_detects_violation(
    tmp_path: Path, rel_path: str, source: str, rule: str, imported_unit: str
) -> None:
    root = _make_backend(tmp_path, {rel_path: source})
    violations = find_violations(root)
    matching = [v for v in violations if v.rule == rule]
    assert matching, f"expected a rule ({rule}) violation, got {violations}"
    v = matching[0]
    assert v.source_file == f"backend/{rel_path}"
    assert v.imported_unit == imported_unit
    message = v.describe()
    assert f"backend/{rel_path}" in message
    assert imported_unit in message


def test_resolver_accepts_allowed_imports(tmp_path: Path) -> None:
    root = _make_backend(
        tmp_path,
        {
            "domain/session.py": "import os\nfrom . import telemetry\nfrom .units import to_canonical\n",
            "domain/telemetry.py": "import numpy as np\nfrom backend.domain import units\n",
            "ingestion/registry.py": (
                "from backend.domain import telemetry\n"
                "from backend.persistence import telemetry_store\n"
                "from . import fitbit, sensorpush\n"
            ),
            "ingestion/fitbit/importer.py": (
                "from ...domain.session import SleepSession\n"
                "from . import timestamps\n"
                "from ..registry import register\n"
            ),
            "processing/aligner.py": "from backend.domain.units import to_canonical\nfrom .resample import x\n",
            "persistence/metadata_store.py": "import sqlite3\nfrom ..domain import errors\nfrom . import data_directory\n",
            "sonification/mapper.py": "from backend.processing import features\n",
            "audio/render.py": "from backend.sonification import mapper\n",
            "api/pipeline.py": (
                "from backend.ingestion import registry\n"
                "from backend.ingestion.fitbit import importer\n"
                "from backend.processing import aligner\n"
                "from backend import audio, sonification\n"
            ),
        },
    )
    assert find_violations(root) == []


def test_every_violation_is_reported(tmp_path: Path) -> None:
    root = _make_backend(
        tmp_path,
        {
            "domain/a.py": "import backend.api\nimport backend.ingestion\n",
            "processing/b.py": "def f():\n    from backend.ingestion.fitbit import importer\n",
        },
    )
    found = {(v.source_file, v.imported_unit, v.rule) for v in find_violations(root)}
    assert found == {
        ("backend/domain/a.py", "api", "a"),
        ("backend/domain/a.py", "api", "e"),
        ("backend/domain/a.py", "ingestion", "a"),
        ("backend/processing/b.py", "ingestion.fitbit", "d"),
    }
