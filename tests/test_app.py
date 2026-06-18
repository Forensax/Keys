from __future__ import annotations

import json

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, inspect, select, text  # noqa: E402

from app.db import Base, SessionLocal, engine, ensure_provider_archive_column  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ConnectivityTest, Provider, ProviderModel  # noqa: E402


def reset_db() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def setup_and_create_provider(client: TestClient, name: str = "Relay") -> int:
    client.post(
        "/setup",
        data={"password": "long-test-password", "confirm_password": "long-test-password"},
    )
    create = client.post(
        "/providers",
        data={
            "name": name,
            "base_url": "https://relay.example/v1",
            "api_key": "sk-test-secret",
            "notes": "primary",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    return int(create.headers["location"].rsplit("/", 1)[-1])


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


def test_archive_column_is_added_to_existing_sqlite_table(tmp_path) -> None:
    old_engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with old_engine.begin() as connection:
        connection.execute(text("CREATE TABLE providers (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL)"))
        connection.execute(text("INSERT INTO providers (name) VALUES ('Legacy')"))

    ensure_provider_archive_column(old_engine)

    assert "archived_at" in {column["name"] for column in inspect(old_engine).get_columns("providers")}
    with old_engine.connect() as connection:
        row = connection.execute(text("SELECT name, archived_at FROM providers")).one()
    assert row.name == "Legacy"
    assert row.archived_at is None
    old_engine.dispose()


def test_archive_and_restore_provider_flow() -> None:
    reset_db()
    client = TestClient(app)
    provider_id = setup_and_create_provider(client)

    archived = client.post(f"/providers/{provider_id}/archive", follow_redirects=False)
    assert archived.status_code == 303
    assert archived.headers["location"] == "/"
    assert f'href="/providers/{provider_id}"' not in client.get("/").text
    archive_page = client.get("/archive")
    assert f'href="/providers/{provider_id}"' in archive_page.text
    assert "永久删除" in archive_page.text

    detail = client.get(f"/providers/{provider_id}")
    assert "已归档，只读" in detail.text
    assert f'/providers/{provider_id}/edit' not in detail.text
    assert f'/providers/{provider_id}/refresh-models' not in detail.text
    assert f'/providers/{provider_id}/test' not in detail.text

    restored = client.post(f"/providers/{provider_id}/restore", follow_redirects=False)
    assert restored.status_code == 303
    assert restored.headers["location"] == "/archive"
    assert f'href="/providers/{provider_id}"' in client.get("/").text
    assert f'href="/providers/{provider_id}"' not in client.get("/archive").text


def test_archived_provider_rejects_mutations_but_allows_copy_and_delete() -> None:
    reset_db()
    client = TestClient(app)
    provider_id = setup_and_create_provider(client)
    client.post(f"/providers/{provider_id}/archive")

    blocked_responses = [
        client.get(f"/providers/{provider_id}/edit", follow_redirects=False),
        client.post(
            f"/providers/{provider_id}",
            data={"name": "Changed", "base_url": "https://changed.example/v1", "api_key": "", "notes": "", "enabled": "on"},
            follow_redirects=False,
        ),
        client.post(f"/providers/{provider_id}/refresh-models", follow_redirects=False),
        client.post(f"/providers/{provider_id}/test", data={"model_id": "gpt-test"}, follow_redirects=False),
    ]
    assert all(response.status_code == 303 for response in blocked_responses)
    assert all(response.headers["location"] == f"/providers/{provider_id}" for response in blocked_responses)

    with SessionLocal() as db:
        provider = db.get(Provider, provider_id)
        assert provider is not None
        assert provider.name == "Relay"
        assert db.scalar(select(ConnectivityTest).where(ConnectivityTest.provider_id == provider_id)) is None

    key_response = client.get(f"/providers/{provider_id}/api-key")
    assert key_response.status_code == 200
    assert key_response.json()["api_key"] == "sk-test-secret"

    deleted = client.post(f"/providers/{provider_id}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/archive"
    with SessionLocal() as db:
        assert db.get(Provider, provider_id) is None


def test_export_and_import_preserve_archive_state_and_support_old_json() -> None:
    reset_db()
    client = TestClient(app)
    provider_id = setup_and_create_provider(client)
    client.post(f"/providers/{provider_id}/archive")

    exported = client.post("/export", data={"password": ""})
    assert exported.status_code == 200
    assert exported.json()["providers"][0]["archived_at"] is not None

    reset_db()
    import_client = TestClient(app)
    import_client.post(
        "/setup",
        data={"password": "long-test-password", "confirm_password": "long-test-password"},
    )
    payload = {
        "version": 1,
        "contains_secrets": True,
        "providers": [
            {
                "name": "Archived Relay",
                "base_url": "https://archived.example/v1",
                "api_key": "sk-archived",
                "enabled": True,
                "archived_at": "2026-06-18T08:30:00+00:00",
            },
            {
                "name": "Legacy Active Relay",
                "base_url": "https://active.example/v1",
                "api_key": "sk-active",
                "enabled": True,
            },
        ],
    }
    imported = import_client.post(
        "/import",
        files={"file": ("backup.json", json.dumps(payload).encode("utf-8"), "application/json")},
        follow_redirects=False,
    )
    assert imported.status_code == 303
    home = import_client.get("/").text
    archive = import_client.get("/archive").text
    assert "Legacy Active Relay" in home
    assert "Archived Relay" not in home
    assert "Archived Relay" in archive
    assert "Legacy Active Relay" not in archive
