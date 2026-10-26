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


def test_router_schema_preserves_validation_version_and_distinct_authority(tmp_path):
    import pytest
    from pydantic import ValidationError

    from millrace_os.contracts import RouterRead, RouterValidationContext

    schemas, _ = schema_artifacts()
    context = schemas["RouterValidationContext"]
    assert "run_state_version" in context["properties"]
    assert context["additionalProperties"] is False
    valid = {
        "kind": "run",
        "instance_id": "instance",
        "run_id": "run",
        "settings_version": 1,
        "run_state_version": 7,
    }
    assert RouterValidationContext.model_validate(valid).model_dump() == valid
    with pytest.raises(ValidationError):
        RouterValidationContext.model_validate({**valid, "run_state_version": -1})
    read = RouterRead[dict](
        home="default",
        data={"planes": {"main": {"status": "paused"}}, "state_version": 7},
        observed_at="2026-10-21T00:00:00+00:00",
    ).model_dump()
    assert read["authority"] == "core-state-home" and read["source_provider"] == "core"
    assert read["data"]["planes"]["main"]["status"] == "paused"
    assert not {"redacted", "foundation_only"} & read.keys()
    service, _, _ = setup(tmp_path)
    document = __import__("json").loads(export_artifacts(service.app)["openapi.json"])
    paths = document["paths"]
    assert "/api/v1/router/requests/{request_id}" in paths
    assert "/api/v1/router/drafts/{draft_id}/apply" in paths
    assert paths["/api/v1/router/validate"]["post"]["responses"]["200"]


def test_call_v1_schema_stays_exact_with_separate_v2():
    import pytest
    from pydantic import ValidationError

    from millrace_os.contracts import (
        RouterCallObservation,
        RouterCallObservationV2,
        RouterObservationCapabilities,
    )

    schemas, registry = schema_artifacts()
    # Frozen strict-v1 digest from the admitted preimage registry.
    entry = next(
        e for e in registry.schemas if e.contract_id == "RouterCallObservation"
    )
    assert entry.sha256 == (
        "1f4a01942f393c5669a959e0c49919f3a28f9c9e5da91605bd344b6dd58cc4a7"
    )
    assert "holder_joined" not in schemas["RouterCallObservation"]["properties"]
    v2 = schemas["RouterCallObservationV2"]
    assert "holder_joined" in v2["required"]
    assert v2["additionalProperties"] is False
    payload = {
        "home": "default", "request_id": "call", "request_digest": None,
        "instance_id": None, "run_id": None, "source": "core_receipt",
        "phase": "receipt_observed", "core_receipt": None,
        "observed_at": "2026-10-25T00:00:00+00:00",
    }
    for joined in (True, False, None):
        raw = {
            **payload, "contract": "millrace-os-router-call/v2",
            "holder_joined": joined,
        }
        assert RouterCallObservationV2.model_validate(raw).holder_joined is joined
        with pytest.raises(ValidationError):
            RouterCallObservation.model_validate(raw)
    for extra in (
        {}, {"holder_joined": 1}, {"holder_joined": "true"},
        {"holder_joined": True, "extra": 1},
    ):
        with pytest.raises(ValidationError):
            RouterCallObservationV2.model_validate({**payload, **extra})
    with pytest.raises(ValidationError):
        RouterObservationCapabilities.model_validate({
            "home": "default", "observation_contracts": [],
            "holder_joined_supported": 1,
        })
