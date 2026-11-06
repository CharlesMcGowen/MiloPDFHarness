from pathlib import Path

from fastapi.testclient import TestClient

from milo.api import create_app
from milo.config import Settings


def test_api_requires_configured_token(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'milo.sqlite3'}",
        input_root=tmp_path / "input",
        output_root=tmp_path / "output",
        max_upload_mb=5,
        quality_threshold=0.5,
        max_reprocess_attempts=3,
        llm_base_url=None,
        llm_model=None,
        llm_api_key=None,
        api_token="local-test-token",  # noqa: S106 - non-secret test fixture
    )
    client = TestClient(create_app(settings))

    assert client.get("/health").status_code == 200
    assert client.get("/v1/stats").status_code == 401
    response = client.get("/v1/stats", headers={"Authorization": "Bearer local-test-token"})
    assert response.status_code == 200
