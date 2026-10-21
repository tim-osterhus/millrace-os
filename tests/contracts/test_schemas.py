import hashlib
from importlib.resources import files

from test_auth import setup

from millrace_os.api import export_artifacts
from millrace_os.contracts import canonical, schema_artifacts


def test_deterministic_schema_artifacts_and_digest_route(tmp_path):
    service, client, bearer = setup(tmp_path)
    first = export_artifacts(service.app)
    assert first == export_artifacts(service.app)
    for name, data in first.items():
        assert files("millrace_os").joinpath("schemas", name).read_bytes() == data
    schemas, registry = schema_artifacts()
    for entry in registry.schemas:
        assert (
            hashlib.sha256(canonical(schemas[entry.contract_id])).hexdigest()
            == entry.sha256
        )
        response = client.get(
            f"/api/v1/contracts/{entry.contract_id}/1/{entry.sha256}", headers=bearer
        )
        assert response.status_code == 200
        assert (
            response.json()["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        )
    assert (
        client.get("/api/v1/contracts/Error/1/" + "0" * 64, headers=bearer).status_code
        == 404
    )
