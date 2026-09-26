"""Per-environment resolution and version matrix.

Given the same package index, root requirements, locks and attempt budget
as ``/resolve`` plus a list of named environments (each with its own PEP
508 marker environment), this module resolves the root requirements and
their transitive dependencies independently for every environment and
cross-tabulates the successful outcomes.

Isolation
---------
Each environment is solved with its own ``marker_env`` overlay and its own
fresh ``max_attempts`` budget.  Nothing solved for one environment leaks
into another, so an environment that conflicts or hits the search limit
neither fails nor shortens the budget of the rest: every environment keeps
its own ``ok`` / ``conflict`` / ``limit`` outcome and ``attempts`` count.

Matrix
------
Only successful environments participate in the version matrix.  A matrix
cell holds the package's resolved version in that environment, or ``None``
(JSON ``null``) when the package is absent there (not selected at all) —
absence is represented distinctly from every concrete version.
``differences`` lists packages whose version or presence is not identical
across every successful environment; with fewer than two successful
environments no difference can exist and the list is empty.

Environments appear everywhere in input order; packages are sorted by
canonical name, so identical input always yields identical output.
"""

from __future__ import annotations

from typing import Any, Optional

from packaging.version import Version

from .resolver import PackageIndex, RootEntry, resolve
from .serialize import resolution_body


def _build_matrix(
    successful: list[tuple[str, dict[str, str]]],
) -> dict[str, Any]:
    """Cross-tabulate resolved versions over the successful environments.

    ``successful`` holds ``(environment name, resolved mapping)`` pairs in
    input order.  A package absent from an environment renders as ``None``,
    never as a version string, so presence differences are explicit.
    """
    env_names = [name for name, _resolved in successful]
    by_package: dict[str, dict[str, str]] = {}
    for name, resolved in successful:
        for package, version in resolved.items():
            by_package.setdefault(package, {})[name] = version

    packages: list[dict[str, Any]] = []
    differences: list[dict[str, Any]] = []
    for package in sorted(by_package):
        cells = by_package[package]
        versions: dict[str, Optional[str]] = {
            env: cells.get(env) for env in env_names
        }
        entry = {"package": package, "versions": versions}
        packages.append(entry)
        # At least two distinct cells (a concrete version mismatch, or
        # absence in some but not all environments) mark a real difference.
        if len(set(versions.values())) > 1:
            differences.append(entry)

    return {
        "environments": env_names,
        "packages": packages,
        "differences": differences,
    }


def run_environment_matrix(
    index: PackageIndex,
    entries: list[RootEntry],
    locks: dict[str, Version],
    environments: list[tuple[str, dict[str, str]]],
    *,
    allow_prereleases: bool,
    max_attempts: int,
) -> dict[str, Any]:
    """Resolve every named environment independently and render the body.

    Each environment keeps the full public ``/resolve`` response shape
    (prefixed with its ``name``), including its own ``attempts`` and
    conflict/limit report; ``limit`` in one environment never aborts the
    remaining environments.
    """
    results: list[dict[str, Any]] = []
    successful: list[tuple[str, dict[str, str]]] = []

    for name, marker_env in environments:
        result = resolve(
            index,
            [entry.dep for entry in entries],
            locks,
            allow_prereleases=allow_prereleases,
            marker_env=marker_env,
            max_attempts=max_attempts,
        )
        body, _status = resolution_body(
            result,
            allow_prereleases=allow_prereleases,
            max_attempts=max_attempts,
        )
        results.append({"name": name, **body})
        if result.status == "ok":
            successful.append((name, result.chosen))

    statuses = {entry["status"] for entry in results}
    if statuses == {"ok"}:
        status = "ok"
    elif "limit" in statuses:
        # A search-limit result is never a proof of conflict, so it takes
        # precedence over plain conflicts in the overall summary status.
        status = "limit"
    else:
        status = "conflict"

    summary = {
        "environments": len(results),
        "ok": sum(1 for entry in results if entry["status"] == "ok"),
        "conflict": sum(1 for entry in results if entry["status"] == "conflict"),
        "limit": sum(1 for entry in results if entry["status"] == "limit"),
    }

    return {
        "status": status,
        "allow_prereleases": allow_prereleases,
        "summary": summary,
        "environments": results,
        "matrix": _build_matrix(successful),
    }
