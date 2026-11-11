"""Owned hooks vary availability, never the stock app's fixed API schema."""

from millrace_os.api import export_artifacts
from millrace_os.config import ApplicationConfig
from millrace_os.service import Service, ServiceDependencies


def test_owned_and_ordinary_profiles_export_identical_contracts(tmp_path):
    config = ApplicationConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        credential_path=tmp_path / "config" / "credential",
    )
    ordinary = Service(config)
    owned = Service(
        config,
        dependencies=ServiceDependencies(
            demo_context_reader=lambda: {}, demo_finish_request=lambda: None
        ),
    )
    assert export_artifacts(ordinary.app) == export_artifacts(owned.app)
    paths = ordinary.app.openapi()["paths"]
    assert "/api/v1/demo/context" in paths
    assert "/api/v1/demo/finish" in paths
