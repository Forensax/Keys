from __future__ import annotations

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ConnectivityTest, ProviderModel  # noqa: E402


def reset_db() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_setup_login_and_provider_crud() -> None:
    reset_db()
    client = TestClient(app)

    setup_page = client.get("/setup")
    assert setup_page.status_code == 200

    response = client.post(
        "/setup",
        data={"password": "long-test-password", "confirm_password": "long-test-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    create = client.post(
        "/providers",
        data={
            "name": "Relay",
            "base_url": "https://relay.example/v1/",
            "api_key": "sk-test-secret",
            "notes": "primary",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303

    with TestClient(app) as fresh_client:
        login = fresh_client.post("/login", data={"password": "long-test-password"}, follow_redirects=False)
        assert login.status_code == 303
        index = fresh_client.get("/")
        assert "Relay" in index.text


def test_export_without_secrets_omits_api_key() -> None:
    reset_db()
    client = TestClient(app)
    client.post("/setup", data={"password": "long-test-password", "confirm_password": "long-test-password"})
    client.post(
        "/providers",
        data={
            "name": "Relay",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-test-secret",
            "notes": "",
            "enabled": "on",
        },
    )

    response = client.post("/export", data={"password": ""})

    assert response.status_code == 200
    assert response.json()["contains_secrets"] is False
    assert "api_key" not in response.json()["providers"][0]
    assert "sk-test-secret" not in response.text


def test_provider_api_key_endpoint_returns_secret_for_logged_in_session() -> None:
    reset_db()
    client = TestClient(app)
    client.post("/setup", data={"password": "long-test-password", "confirm_password": "long-test-password"})
    create = client.post(
        "/providers",
        data={
            "name": "Relay",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-test-secret",
            "notes": "",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    provider_id = create.headers["location"].rsplit("/", 1)[-1]

    response = client.get(f"/providers/{provider_id}/api-key")

    assert response.status_code == 200
    assert response.json() == {"api_key": "sk-test-secret"}


def test_provider_api_key_endpoint_requires_login() -> None:
    reset_db()
    client = TestClient(app)
    client.post("/setup", data={"password": "long-test-password", "confirm_password": "long-test-password"})
    create = client.post(
        "/providers",
        data={
            "name": "Relay",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-test-secret",
            "notes": "",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    provider_id = create.headers["location"].rsplit("/", 1)[-1]

    fresh_client = TestClient(app)
    response = fresh_client.get(f"/providers/{provider_id}/api-key", headers={"accept": "application/json"})

    assert response.status_code == 401


def test_index_uses_latest_valid_model_then_falls_back_to_first() -> None:
    reset_db()
    client = TestClient(app)
    client.post("/setup", data={"password": "long-test-password", "confirm_password": "long-test-password"})

    provider_ids: list[int] = []
    for index in range(3):
        create = client.post(
            "/providers",
            data={
                "name": f"Relay {index}",
                "base_url": f"https://relay-{index}.example/v1",
                "api_key": f"sk-test-secret-{index}",
                "notes": "",
                "enabled": "on",
            },
            follow_redirects=False,
        )
        provider_ids.append(int(create.headers["location"].rsplit("/", 1)[-1]))

    with SessionLocal() as db:
        for provider_id in provider_ids:
            db.add_all(
                [
                    ProviderModel(provider_id=provider_id, model_id="model-a", owned_by="test", raw_json="{}"),
                    ProviderModel(provider_id=provider_id, model_id="model-b", owned_by="test", raw_json="{}"),
                ]
            )
        db.add(ConnectivityTest(provider_id=provider_ids[0], model_id="model-b", status="success"))
        db.add(ConnectivityTest(provider_id=provider_ids[1], model_id="removed-model", status="success"))
        db.commit()

    response = client.get("/")

    assert response.status_code == 200
    assert f'data-provider-id="{provider_ids[0]}" data-default-model="model-b"' in " ".join(response.text.split())
    assert f'data-provider-id="{provider_ids[1]}" data-default-model="model-a"' in " ".join(response.text.split())
    assert f'data-provider-id="{provider_ids[2]}" data-default-model="model-a"' in " ".join(response.text.split())
    assert 'class="name-cell"' in response.text
    assert 'class="test-line"' in response.text
    assert 'class="action-cell"' in response.text
    assert '<div class="actions">' in response.text
