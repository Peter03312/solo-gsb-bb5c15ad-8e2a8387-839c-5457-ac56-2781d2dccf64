"""End-to-end tests for ``POST /analyze/environment-matrix``.

Each environment is resolved independently with its own PEP 508 marker
environment and a fresh attempt budget; the successful environments are
then cross-tabulated into a version matrix that distinguishes concrete
versions from absence (``null``).
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _matrix(**payload):
    response = client.post("/analyze/environment-matrix", json=payload)
    return response.status_code, response.json()


PLATFORM_PACKAGES = {
    "app": {
        "1.0": {
            "dependencies": [
                "lib>=1.0",
                "plat<2.0; sys_platform == 'linux'",
                "plat>=2.0; sys_platform == 'win32'",
                "winonly; sys_platform == 'win32'",
            ]
        }
    },
    "lib": {"1.0": {}, "2.0": {}},
    "plat": {"1.0": {}, "2.0": {}},
    "winonly": {"1.0": {}},
}

LINUX = {"name": "linux", "marker_env": {"sys_platform": "linux"}}
WINDOWS = {"name": "windows", "marker_env": {"sys_platform": "win32"}}


def test_matrix_resolves_each_environment_and_tabulates_versions():
    status, body = _matrix(
        packages=PLATFORM_PACKAGES,
        root=["app"],
        environments=[LINUX, WINDOWS],
    )
    assert status == 200
    assert body["status"] == "ok"
    assert body["summary"] == {"environments": 2, "ok": 2, "conflict": 0, "limit": 0}
    assert body["allow_prereleases"] is False

    environments = body["environments"]
    assert [env["name"] for env in environments] == ["linux", "windows"]
    assert environments[0]["status"] == "ok"
    assert environments[0]["attempts"] == 3
    assert environments[0]["resolved"] == {
        "app": "1.0", "lib": "2.0", "plat": "1.0"
    }
    assert environments[1]["attempts"] == 4
    assert environments[1]["resolved"] == {
        "app": "1.0", "lib": "2.0", "plat": "2.0", "winonly": "1.0"
    }
    # Each successful entry keeps the full /resolve response shape.
    for env in environments:
        assert "dependency_graph" in env and isinstance(env["dependency_graph"], list)
        assert env["locks"] == {}

    matrix = body["matrix"]
    assert matrix["environments"] == ["linux", "windows"]
    assert [entry["package"] for entry in matrix["packages"]] == [
        "app", "lib", "plat", "winonly"
    ]
    by_package = {entry["package"]: entry["versions"] for entry in matrix["packages"]}
    # Identical versions across environments.
    assert by_package["app"] == {"linux": "1.0", "windows": "1.0"}
    assert by_package["lib"] == {"linux": "2.0", "windows": "2.0"}
    # Version difference.
    assert by_package["plat"] == {"linux": "1.0", "windows": "2.0"}
    # Absence (null) is distinct from any concrete version.
    assert by_package["winonly"] == {"linux": None, "windows": "1.0"}
    assert [entry["package"] for entry in matrix["differences"]] == ["plat", "winonly"]


def test_environments_keep_input_order_everywhere():
    status, body = _matrix(
        packages=PLATFORM_PACKAGES,
        root=["app"],
        environments=[WINDOWS, LINUX],
    )
    assert status == 200
    assert [env["name"] for env in body["environments"]] == ["windows", "linux"]
    assert body["matrix"]["environments"] == ["windows", "linux"]
    plat = next(p for p in body["matrix"]["packages"] if p["package"] == "plat")
    assert list(plat["versions"]) == ["windows", "linux"]
    assert plat["versions"] == {"windows": "2.0", "linux": "1.0"}


def test_marker_env_defaults_to_the_fixed_resolver_environment():
    # No marker_env override -> DEFAULT_MARKER_ENV (sys_platform=linux,
    # python_version=3.11), so the linux-marked dependency applies.
    status, body = _matrix(
        packages=PLATFORM_PACKAGES,
        root=["app"],
        environments=[{"name": "default"}, WINDOWS],
    )
    assert status == 200
    default_env = body["environments"][0]
    assert default_env["resolved"]["plat"] == "1.0"
    assert "winonly" not in default_env["resolved"]


def test_conflict_in_one_environment_is_isolated():
    packages = {
        "app": {
            "1.0": {
                "dependencies": [
                    "lib>=2.0; python_version >= '3.11'",
                    "lib<2.0; python_version < '3.11'",
                ]
            }
        },
        "lib": {"1.0": {}},
    }
    status, body = _matrix(
        packages=packages,
        root=["app"],
        environments=[
            {"name": "modern", "marker_env": {"python_version": "3.11"}},
            {"name": "legacy", "marker_env": {"python_version": "3.10"}},
        ],
    )
    assert status == 200
    assert body["status"] == "conflict"
    assert body["summary"] == {"environments": 2, "ok": 1, "conflict": 1, "limit": 0}

    modern, legacy = body["environments"]
    assert modern["name"] == "modern"
    assert modern["status"] == "conflict"
    terminals = modern["conflict"]["terminal_conflicts"]
    assert terminals[0]["package"] == "lib"
    assert legacy["status"] == "ok"
    assert legacy["resolved"] == {"app": "1.0", "lib": "1.0"}
    assert legacy["attempts"] == 2

    # Only successful environments feed the matrix; <2 successes means no
    # differences, even though the conflicting environment would differ.
    matrix = body["matrix"]
    assert matrix["environments"] == ["legacy"]
    assert [entry["package"] for entry in matrix["packages"]] == ["app", "lib"]
    assert matrix["differences"] == []


def test_limit_in_one_environment_does_not_shrink_other_budgets():
    packages = {}
    for i in range(3):
        packages[f"p{i}"] = {
            f"{major}.0": {"dependencies": {f"p{i+1}": ""}}
            for major in range(1, 4)
        }
    packages["p3"] = {f"{major}.0": {} for major in range(1, 4)}
    packages["simple"] = {"1.0": {}}

    status, body = _matrix(
        packages=packages,
        root=["p0; python_version >= '3.11'", "simple"],
        max_attempts=3,
        environments=[
            {"name": "heavy", "marker_env": {"python_version": "3.11"}},
            {"name": "light", "marker_env": {"python_version": "3.10"}},
        ],
    )
    assert status == 200
    assert body["status"] == "limit"
    assert body["summary"] == {"environments": 2, "ok": 1, "conflict": 0, "limit": 1}

    heavy, light = body["environments"]
    assert heavy["status"] == "limit"
    assert heavy["attempts"] == 4
    assert heavy["error"]["code"] == "search_limit_exceeded"
    assert "conflict" in heavy
    # The light environment got its own full budget and still solved.
    assert light["status"] == "ok"
    assert light["attempts"] == 1
    assert light["resolved"] == {"simple": "1.0"}

    assert body["matrix"]["environments"] == ["light"]
    assert body["matrix"]["differences"] == []


def test_all_environments_conflicting_yields_empty_matrix():
    packages = {
        "app": {
            "1.0": {
                "dependencies": [
                    "lib>=2.0; python_version >= '3.11'",
                    "lib>=3.0; python_version < '3.11'",
                ]
            }
        },
        "lib": {"1.0": {}},
    }
    status, body = _matrix(
        packages=packages,
        root=["app"],
        environments=[
            {"name": "modern", "marker_env": {"python_version": "3.11"}},
            {"name": "legacy", "marker_env": {"python_version": "3.10"}},
        ],
    )
    assert status == 200
    assert body["status"] == "conflict"
    assert body["summary"]["conflict"] == 2
    assert body["matrix"] == {
        "environments": [], "packages": [], "differences": []
    }


def test_limit_status_takes_precedence_over_conflict():
    packages = {
        "app": {
            "1.0": {
                "dependencies": [
                    "ghost>=1.0; python_version < '3.11'",
                ]
            }
        },
    }
    # heavy hits the limit (wide chain present only on 3.11), legacy has an
    # unsatisfiable root on 3.10 via a missing transitive package.
    for i in range(3):
        packages[f"p{i}"] = {
            f"{major}.0": {"dependencies": {f"p{i+1}": ""}}
            for major in range(1, 4)
        }
    packages["p3"] = {f"{major}.0": {} for major in range(1, 4)}
    packages["app"]["1.0"]["dependencies"].append(
        "p0; python_version >= '3.11'"
    )
    status, body = _matrix(
        packages=packages,
        root=["app"],
        max_attempts=3,
        environments=[
            {"name": "legacy", "marker_env": {"python_version": "3.10"}},
            {"name": "heavy", "marker_env": {"python_version": "3.11"}},
        ],
    )
    assert status == 200
    assert {env["status"] for env in body["environments"]} == {"conflict", "limit"}
    assert body["status"] == "limit"
    assert body["summary"] == {"environments": 2, "ok": 0, "conflict": 1, "limit": 1}


def test_duplicate_environment_names_rejected_with_400():
    status, body = _matrix(
        packages={"x": {"1.0": {}}},
        root={"x": ""},
        environments=[
            {"name": "same", "marker_env": {"python_version": "3.10"}},
            {"name": "same", "marker_env": {"python_version": "3.11"}},
        ],
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    assert "unique" in body["error"]["message"]


def test_empty_environment_name_rejected_with_400():
    for name in ("", "   "):
        status, body = _matrix(
            packages={"x": {"1.0": {}}},
            root={"x": ""},
            environments=[
                {"name": name},
                {"name": "other"},
            ],
        )
        assert status == 400
        assert body["error"]["code"] == "invalid_request"


def test_environment_count_must_be_between_two_and_eight():
    base_env = {"name": "e", "marker_env": {}}
    too_few = client.post(
        "/analyze/environment-matrix",
        json={"packages": {"x": {"1.0": {}}}, "root": {"x": ""},
              "environments": [base_env]},
    )
    assert too_few.status_code == 422
    too_many = client.post(
        "/analyze/environment-matrix",
        json={
            "packages": {"x": {"1.0": {}}}, "root": {"x": ""},
            "environments": [
                {"name": f"e{i}", "marker_env": {}} for i in range(9)
            ],
        },
    )
    assert too_many.status_code == 422
    # Exactly 2 and 8 are accepted.
    for count in (2, 8):
        response = client.post(
            "/analyze/environment-matrix",
            json={
                "packages": {"x": {"1.0": {}}}, "root": {"x": ""},
                "environments": [
                    {"name": f"e{i}", "marker_env": {}} for i in range(count)
                ],
            },
        )
        assert response.status_code == 200


def test_resolver_input_validation_is_reused():
    # Invalid package index -> same 400 as /resolve.
    status, body = _matrix(
        packages={"x": {"not-a-version": {}}},
        root={"x": ""},
        environments=[LINUX, WINDOWS],
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    assert "PEP 440" in body["error"]["message"]


def test_extra_fields_rejected():
    # Unknown environment field.
    response = client.post(
        "/analyze/environment-matrix",
        json={
            "packages": {"x": {"1.0": {}}}, "root": {"x": ""},
            "environments": [
                {"name": "a", "marker_env": {}, "unexpected": 1},
                {"name": "b", "marker_env": {}},
            ],
        },
    )
    assert response.status_code == 422
    # Unknown top-level field (including a top-level marker_env, which is
    # deliberately per-environment on this endpoint).
    response = client.post(
        "/analyze/environment-matrix",
        json={
            "packages": {"x": {"1.0": {}}}, "root": {"x": ""},
            "environments": [
                {"name": "a"}, {"name": "b"}
            ],
            "marker_env": {"python_version": "3.11"},
        },
    )
    assert response.status_code == 422


def test_allow_prereleases_applies_to_every_environment():
    status, body = _matrix(
        packages={"x": {"1.0a1": {}}},
        root={"x": ""},
        allow_prereleases=True,
        environments=[
            {"name": "a", "marker_env": {"python_version": "3.10"}},
            {"name": "b", "marker_env": {"python_version": "3.11"}},
        ],
    )
    assert status == 200
    assert body["status"] == "ok"
    assert all(env["resolved"] == {"x": "1.0a1"} for env in body["environments"])
    assert body["matrix"]["differences"] == []


def test_matrix_is_deterministic():
    payload = {
        "packages": PLATFORM_PACKAGES,
        "root": ["app"],
        "environments": [WINDOWS, LINUX],
    }
    responses = [
        client.post("/analyze/environment-matrix", json=payload).json()
        for _ in range(3)
    ]
    assert all(response == responses[0] for response in responses)
