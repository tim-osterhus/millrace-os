from importlib.resources import files as package_files

from test_auth import bootstrap, setup

import millrace_os.api as api


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


def test_recovery_bootstrap_survives_missing_normal_asset(tmp_path, monkeypatch):
    service, client, _ = setup(tmp_path, damaged=True)
    normal_files = api.files

    class NormalAssetRemoved:
        def __init__(self, root):
            self.root = root

        def joinpath(self, *parts):
            if parts == ("assets", "bootstrap.js"):
                raise FileNotFoundError("normal bootstrap removed")
            return self.root.joinpath(*parts)

    def resources(package):
        root = normal_files(package)
        if package == "millrace_os":
            return NormalAssetRemoved(root)
        return root

    monkeypatch.setattr(api, "files", resources)
    assert client.get("/assets/bootstrap.js").status_code == 503
    response = client.get("/recovery/bootstrap.js")
    assert response.status_code == 200
    assert response.text.startswith('"use strict";')
    assert package_files("millrace_os.recovery").joinpath("bootstrap.js").is_file()
