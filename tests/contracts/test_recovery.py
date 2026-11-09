from importlib.resources import files as package_files

from test_auth import bootstrap, setup

import millrace_os.frontend as frontend


def test_independent_shell_cold_exchange_and_closed_normal_api(tmp_path):
    service, client, bearer = setup(tmp_path, damaged=True)
    original = service.config.database_path.read_bytes()
    assert client.get("/health").status_code == 503
    shell = client.get("/recovery")
    assert shell.status_code == 200
    assert "/recovery/bootstrap.js" in shell.text
    assert "/assets/" not in shell.text and "PRIVATE" not in shell.text
    assert client.get("/api/v1/recovery/diagnostics").status_code == 401
    assert (
        client.post("/api/v1/auth/bootstrap", json={}, headers=bearer).status_code
        == 503
    )
    bootstrap(client, bearer, recovery=True)
    diagnostics = client.get("/api/v1/recovery/diagnostics")
    assert diagnostics.status_code == 200
    assert diagnostics.json()["data"]["storage_error"] == "corrupt_state"
    assert "PRIVATE" not in diagnostics.text
    assert client.get("/api/v1/version").status_code == 403
    assert client.get("/api/v1/version", headers=bearer).status_code == 503
    assert service.config.database_path.read_bytes() == original


def test_recovery_bootstrap_survives_missing_or_changed_dashboard(
    tmp_path, monkeypatch
):
    _, client, _ = setup(tmp_path, damaged=True)
    normal_files = frontend.files
    recovery_html = client.get("/recovery").text
    recovery_js = client.get("/recovery/bootstrap.js").text

    class DashboardRemoved:
        def __init__(self, root):
            self.root = root

        def joinpath(self, *parts):
            # The stock frontend resolves its closure one component at a time.
            if parts[:1] == ("assets",):
                raise FileNotFoundError("normal dashboard removed")
            return self.root.joinpath(*parts)

    def without_dashboard(package):
        return DashboardRemoved(normal_files(package))

    monkeypatch.setattr(frontend, "files", without_dashboard)
    for path in ("/ui/stock/", "/ui/stock/index.html"):
        response = client.get(path)
        assert response.status_code == 503
        assert response.json()["code"] == "frontend_unavailable"
    response = client.get("/recovery/bootstrap.js")
    assert response.status_code == 200
    assert response.text == recovery_js and response.text.startswith('"use strict";')
    assert client.get("/recovery").text == recovery_html
    assert package_files("millrace_os.recovery").joinpath("bootstrap.js").is_file()

    changed = tmp_path / "changed"
    (changed / "assets").mkdir(parents=True)
    # An invalid manifest never falls back to serving a partial closure.
    (changed / "assets" / "dashboard").mkdir()
    (changed / "assets" / "dashboard" / frontend.MANIFEST_NAME).write_text("{}")
    monkeypatch.setattr(frontend, "files", lambda package: changed)
    assert client.get("/ui/stock/").json()["code"] == "frontend_unavailable"
    assert client.get("/recovery").status_code == 200
    assert client.get("/recovery/bootstrap.js").status_code == 200
