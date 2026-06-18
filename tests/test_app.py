from __future__ import annotations

import json

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, inspect, select, text  # noqa: E402

from app import main as main_module  # noqa: E402
from app.db import (  # noqa: E402
    Base,
    SessionLocal,
    engine,
    ensure_client_profile_columns,
    ensure_provider_archive_column,
    ensure_proxy_columns,
    ensure_test_preference_columns,
)
from app.main import app  # noqa: E402
from app.models import ConnectivityTest, NetworkProxy, Provider, ProviderModel  # noqa: E402
from app.openai_compat import (  # noqa: E402
    CLIENT_PROFILE_CLAUDE_CODE,
    CLIENT_PROFILE_CODEX,
    ConnectivityTestResult,
)
from app.proxy_support import ProxyTestResult  # noqa: E402


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


def create_proxy(client: TestClient, name: str = "Local Proxy", enabled: bool = True) -> int:
    data = {
        "name": name,
        "scheme": "socks5h",
        "host": "proxy.example",
        "port": "1080",
        "username": "user@name",
        "password": "p:a/ss",
        "notes": "test proxy",
    }
    if enabled:
        data["enabled"] = "on"
    response = client.post("/proxies", data=data, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as db:
        proxy = db.scalar(select(NetworkProxy).where(NetworkProxy.name == name))
        assert proxy is not None
        return proxy.id


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
            "client_profile": CLIENT_PROFILE_CODEX,
        },
        follow_redirects=False,
    )
    assert create.status_code == 303

    with SessionLocal() as db:
        provider = db.scalar(select(Provider).where(Provider.name == "Relay"))
        assert provider is not None
        assert provider.client_profile == CLIENT_PROFILE_CODEX

    updated = client.post(
        create.headers["location"],
        data={
            "name": "Relay",
            "base_url": "https://relay.example/v1",
            "api_key": "",
            "notes": "updated",
            "enabled": "on",
            "client_profile": CLIENT_PROFILE_CLAUDE_CODE,
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    with SessionLocal() as db:
        provider = db.scalar(select(Provider).where(Provider.name == "Relay"))
        assert provider is not None
        assert provider.client_profile == CLIENT_PROFILE_CLAUDE_CODE

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
    assert response.json()["providers"][0]["client_profile"] == "openai_chat"
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


def test_client_profile_columns_are_added_to_existing_sqlite_tables(tmp_path) -> None:
    old_engine = create_engine(f"sqlite:///{tmp_path / 'old-profiles.db'}")
    with old_engine.begin() as connection:
        connection.execute(text("CREATE TABLE providers (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL)"))
        connection.execute(
            text("CREATE TABLE connectivity_tests (id INTEGER PRIMARY KEY, model_id VARCHAR NOT NULL)")
        )
        connection.execute(text("INSERT INTO providers (name) VALUES ('Legacy')"))
        connection.execute(text("INSERT INTO connectivity_tests (model_id) VALUES ('legacy-model')"))

    ensure_client_profile_columns(old_engine)

    for table_name in ("providers", "connectivity_tests"):
        assert "client_profile" in {column["name"] for column in inspect(old_engine).get_columns(table_name)}
        with old_engine.connect() as connection:
            profile = connection.execute(text(f"SELECT client_profile FROM {table_name}")).scalar_one()
        assert profile == "openai_chat"
    old_engine.dispose()


def test_proxy_columns_are_added_to_existing_sqlite_tables(tmp_path) -> None:
    old_engine = create_engine(f"sqlite:///{tmp_path / 'old-proxy-columns.db'}")
    with old_engine.begin() as connection:
        connection.execute(text("CREATE TABLE providers (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL)"))
        connection.execute(text("CREATE TABLE connectivity_tests (id INTEGER PRIMARY KEY, model_id VARCHAR NOT NULL)"))
        connection.execute(text("INSERT INTO providers (name) VALUES ('Legacy')"))
        connection.execute(text("INSERT INTO connectivity_tests (model_id) VALUES ('legacy-model')"))

    ensure_proxy_columns(old_engine)

    assert "default_proxy_id" in {column["name"] for column in inspect(old_engine).get_columns("providers")}
    assert "network_route" in {
        column["name"] for column in inspect(old_engine).get_columns("connectivity_tests")
    }
    with old_engine.connect() as connection:
        assert connection.execute(text("SELECT default_proxy_id FROM providers")).scalar_one() is None
        assert connection.execute(text("SELECT network_route FROM connectivity_tests")).scalar_one() == "直连"
    old_engine.dispose()


def test_test_preference_columns_are_added_to_existing_provider_table(tmp_path) -> None:
    old_engine = create_engine(f"sqlite:///{tmp_path / 'old-test-preferences.db'}")
    with old_engine.begin() as connection:
        connection.execute(text("CREATE TABLE providers (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL)"))
        connection.execute(text("INSERT INTO providers (name) VALUES ('Legacy')"))

    ensure_test_preference_columns(old_engine)

    columns = {column["name"] for column in inspect(old_engine).get_columns("providers")}
    assert {"test_model_id", "test_client_profile", "test_network_route"}.issubset(columns)
    with old_engine.connect() as connection:
        row = connection.execute(
            text("SELECT test_model_id, test_client_profile, test_network_route FROM providers")
        ).one()
    assert row.test_model_id is None
    assert row.test_client_profile is None
    assert row.test_network_route == "default"
    old_engine.dispose()


def test_detail_allows_manual_model_and_temporary_profile(monkeypatch) -> None:
    reset_db()
    client = TestClient(app)
    provider_id = setup_and_create_provider(client)
    calls: list[tuple[str, str, str | None]] = []

    async def fake_test(
        base_url: str,
        api_key: str,
        model_id: str,
        client_profile: str,
        proxy_url: str | None = None,
    ):
        calls.append((model_id, client_profile, proxy_url))
        return ConnectivityTestResult("success", 12, "", '{"ok":true}')

    monkeypatch.setattr(main_module, "run_connectivity_test", fake_test)

    detail = client.get(f"/providers/{provider_id}")
    assert 'list="provider-models"' in detail.text
    assert "模型缓存为空时，可以直接输入模型名称进行测试。" in detail.text

    tested = client.post(
        f"/providers/{provider_id}/test",
        data={"model_id": "manual-model", "client_profile": CLIENT_PROFILE_CLAUDE_CODE},
        follow_redirects=False,
    )

    assert tested.status_code == 303
    assert calls == [("manual-model", CLIENT_PROFILE_CLAUDE_CODE, None)]
    with SessionLocal() as db:
        provider = db.get(Provider, provider_id)
        test = db.scalar(select(ConnectivityTest).where(ConnectivityTest.provider_id == provider_id))
        assert provider is not None and provider.client_profile == "openai_chat"
        assert provider.test_model_id == "manual-model"
        assert provider.test_client_profile == CLIENT_PROFILE_CLAUDE_CODE
        assert provider.test_network_route == "default"
        assert test is not None and test.client_profile == CLIENT_PROFILE_CLAUDE_CODE


def test_detail_model_rows_fill_test_model_and_archive_stays_read_only() -> None:
    reset_db()
    client = TestClient(app)
    provider_id = setup_and_create_provider(client)
    with SessionLocal() as db:
        provider = db.get(Provider, provider_id)
        assert provider is not None
        provider.test_model_id = "model-b"
        db.add_all(
            [
                ProviderModel(provider_id=provider_id, model_id="model-a", owned_by="test", raw_json="{}"),
                ProviderModel(provider_id=provider_id, model_id="model-b", owned_by="test", raw_json="{}"),
            ]
        )
        db.commit()

    detail = client.get(f"/providers/{provider_id}")

    assert detail.status_code == 200
    assert detail.text.count("data-model-fill") == 2
    assert 'data-model-value="model-a"' in detail.text
    assert 'data-model-value="model-b"' in detail.text
    assert 'data-model-value="model-b" aria-pressed="true"' in " ".join(detail.text.split())
    assert "选择 model-b 作为测试模型" in detail.text

    client.post(f"/providers/{provider_id}/archive")
    archived_detail = client.get(f"/providers/{provider_id}")

    assert archived_detail.status_code == 200
    assert "data-model-fill" not in archived_detail.text
    assert archived_detail.text.count('class="model-row"') == 2


def test_detail_response_column_uses_success_response_and_failure_error() -> None:
    reset_db()
    client = TestClient(app)
    provider_id = setup_and_create_provider(client)
    with SessionLocal() as db:
        db.add_all(
            [
                ConnectivityTest(
                    provider_id=provider_id,
                    model_id="success-model",
                    status="success",
                    raw_response_excerpt='{"output":"pong"}',
                ),
                ConnectivityTest(
                    provider_id=provider_id,
                    model_id="empty-model",
                    status="success",
                    raw_response_excerpt="",
                ),
                ConnectivityTest(
                    provider_id=provider_id,
                    model_id="failed-model",
                    status="failed",
                    error_message="HTTP 400: request rejected",
                    raw_response_excerpt='{"error":"detail"}',
                ),
            ]
        )
        db.commit()

    response = client.get(f"/providers/{provider_id}")

    assert response.status_code == 200
    assert "<th>响应</th>" in response.text
    assert "<th>错误</th>" not in response.text
    assert '{&#34;output&#34;:&#34;pong&#34;}' in response.text
    assert "成功（响应正文为空）" in response.text
    assert "HTTP 400: request rejected" in response.text
    assert '{&#34;error&#34;:&#34;detail&#34;}' not in response.text
    assert response.text.count('data-response-tooltip="') == 3


def test_invalid_client_profile_is_rejected() -> None:
    reset_db()
    client = TestClient(app)
    client.post("/setup", data={"password": "long-test-password", "confirm_password": "long-test-password"})

    response = client.post(
        "/providers",
        data={
            "name": "Relay",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-test",
            "client_profile": "unknown-client",
        },
    )

    assert response.status_code == 400
    assert "客户端模式无效" in response.text


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
                "client_profile": CLIENT_PROFILE_CODEX,
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
    with SessionLocal() as db:
        archived_provider = db.scalar(select(Provider).where(Provider.name == "Archived Relay"))
        legacy_provider = db.scalar(select(Provider).where(Provider.name == "Legacy Active Relay"))
        assert archived_provider is not None and archived_provider.client_profile == CLIENT_PROFILE_CODEX
        assert legacy_provider is not None and legacy_provider.client_profile == "openai_chat"


def test_import_rejects_invalid_client_profile_without_stopping_other_rows() -> None:
    reset_db()
    client = TestClient(app)
    client.post("/setup", data={"password": "long-test-password", "confirm_password": "long-test-password"})
    payload = {
        "providers": [
            {
                "name": "Invalid Relay",
                "base_url": "https://invalid.example/v1",
                "api_key": "sk-invalid",
                "client_profile": "made-up-client",
            },
            {
                "name": "Valid Relay",
                "base_url": "https://valid.example/v1",
                "api_key": "sk-valid",
                "client_profile": CLIENT_PROFILE_CLAUDE_CODE,
            },
        ]
    }

    response = client.post(
        "/import",
        files={"file": ("backup.json", json.dumps(payload).encode("utf-8"), "application/json")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with SessionLocal() as db:
        assert db.scalar(select(Provider).where(Provider.name == "Invalid Relay")) is None
        valid = db.scalar(select(Provider).where(Provider.name == "Valid Relay"))
        assert valid is not None and valid.client_profile == CLIENT_PROFILE_CLAUDE_CODE


def test_proxy_credentials_are_encrypted_and_proxy_page_masks_them() -> None:
    reset_db()
    client = TestClient(app)
    client.post("/setup", data={"password": "long-test-password", "confirm_password": "long-test-password"})
    proxy_id = create_proxy(client)

    with SessionLocal() as db:
        proxy = db.get(NetworkProxy, proxy_id)
        assert proxy is not None
        assert "user@name" not in proxy.encrypted_username
        assert "p:a/ss" not in proxy.encrypted_password
        assert proxy.has_auth is True

    page = client.get("/proxies")
    assert page.status_code == 200
    assert "socks5h://proxy.example:1080" in page.text
    assert "有认证" in page.text
    assert "user@name" not in page.text
    assert "p:a/ss" not in page.text
    assert '>代理</a>' in client.get("/").text


def test_provider_default_and_temporary_proxy_routes(monkeypatch) -> None:
    reset_db()
    client = TestClient(app)
    client.post("/setup", data={"password": "long-test-password", "confirm_password": "long-test-password"})
    proxy_id = create_proxy(client)
    create = client.post(
        "/providers",
        data={
            "name": "Proxy Relay",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-test-secret",
            "enabled": "on",
            "default_proxy_id": str(proxy_id),
        },
        follow_redirects=False,
    )
    provider_id = int(create.headers["location"].rsplit("/", 1)[-1])
    calls: list[str | None] = []

    async def fake_test(*args, proxy_url: str | None = None, **kwargs):
        calls.append(proxy_url)
        return ConnectivityTestResult("success", 9, "", '{"ok":true}')

    monkeypatch.setattr(main_module, "run_connectivity_test", fake_test)
    client.post(f"/providers/{provider_id}/test", data={"model_id": "model-default"})
    client.post(
        f"/providers/{provider_id}/test",
        data={"model_id": "model-direct", "network_route": "direct"},
    )

    assert calls[0] == "socks5h://user%40name:p%3Aa%2Fss@proxy.example:1080"
    assert calls[1] is None
    with SessionLocal() as db:
        provider = db.get(Provider, provider_id)
        routes = list(
            db.scalars(
                select(ConnectivityTest.network_route)
                .where(ConnectivityTest.provider_id == provider_id)
                .order_by(ConnectivityTest.id)
            ).all()
        )
        assert provider is not None and provider.default_proxy_id == proxy_id
        assert routes == ["Local Proxy", "直连"]


def test_disabled_default_proxy_rejects_requests_without_direct_fallback(monkeypatch) -> None:
    reset_db()
    client = TestClient(app)
    client.post("/setup", data={"password": "long-test-password", "confirm_password": "long-test-password"})
    proxy_id = create_proxy(client)
    create = client.post(
        "/providers",
        data={
            "name": "Proxy Relay",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-test-secret",
            "enabled": "on",
            "default_proxy_id": str(proxy_id),
        },
        follow_redirects=False,
    )
    provider_id = int(create.headers["location"].rsplit("/", 1)[-1])
    client.post(f"/proxies/{proxy_id}/toggle")
    called = False

    async def should_not_call(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("不应发起外部请求")

    monkeypatch.setattr(main_module, "fetch_models", should_not_call)
    monkeypatch.setattr(main_module, "run_connectivity_test", should_not_call)
    refresh = client.post(f"/providers/{provider_id}/refresh-models", follow_redirects=True)
    tested = client.post(
        f"/providers/{provider_id}/test",
        data={"model_id": "model-test"},
        follow_redirects=True,
    )

    assert called is False
    assert "已禁用" in refresh.text
    assert "已禁用" in tested.text
    with SessionLocal() as db:
        test = db.scalar(select(ConnectivityTest).where(ConnectivityTest.provider_id == provider_id))
        assert test is not None
        assert test.status == "failed"
        assert "已禁用" in test.error_message


def test_referenced_proxy_cannot_be_deleted() -> None:
    reset_db()
    client = TestClient(app)
    client.post("/setup", data={"password": "long-test-password", "confirm_password": "long-test-password"})
    proxy_id = create_proxy(client)
    client.post(
        "/providers",
        data={
            "name": "Proxy Relay",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-test-secret",
            "enabled": "on",
            "default_proxy_id": str(proxy_id),
        },
    )

    response = client.post(f"/proxies/{proxy_id}/delete", follow_redirects=True)

    assert "仍有 1 个中转站引用该代理" in response.text
    with SessionLocal() as db:
        assert db.get(NetworkProxy, proxy_id) is not None


def test_proxy_self_test_persists_latest_result(monkeypatch) -> None:
    reset_db()
    client = TestClient(app)
    client.post("/setup", data={"password": "long-test-password", "confirm_password": "long-test-password"})
    proxy_id = create_proxy(client)

    async def fake_proxy_test(proxy_url: str):
        assert proxy_url.startswith("socks5h://user%40name:")
        return ProxyTestResult("success", "203.0.113.10", 23, "")

    monkeypatch.setattr(main_module, "test_proxy_connection", fake_proxy_test)
    response = client.post(f"/proxies/{proxy_id}/test", follow_redirects=True)

    assert "出口 IP：203.0.113.10" in response.text
    with SessionLocal() as db:
        proxy = db.get(NetworkProxy, proxy_id)
        assert proxy is not None
        assert proxy.last_test_status == "success"
        assert proxy.last_exit_ip == "203.0.113.10"
        assert proxy.last_latency_ms == 23
        assert proxy.last_tested_at is not None


def test_proxy_export_and_secret_import_preserve_default_reference() -> None:
    reset_db()
    client = TestClient(app)
    client.post("/setup", data={"password": "long-test-password", "confirm_password": "long-test-password"})
    proxy_id = create_proxy(client)
    client.post(
        "/providers",
        data={
            "name": "Proxy Relay",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-test-secret",
            "enabled": "on",
            "default_proxy_id": str(proxy_id),
        },
    )
    with SessionLocal() as db:
        provider = db.scalar(select(Provider).where(Provider.name == "Proxy Relay"))
        assert provider is not None
        provider.test_model_id = "gpt-batch"
        provider.test_client_profile = CLIENT_PROFILE_CODEX
        provider.test_network_route = f"proxy:{proxy_id}"
        db.commit()

    public_export = client.post("/export", data={"password": ""}).json()
    assert public_export["version"] == 3
    assert public_export["proxies"][0]["has_auth"] is True
    assert "username" not in public_export["proxies"][0]
    assert "password" not in public_export["proxies"][0]
    assert public_export["providers"][0]["default_proxy"] == "Local Proxy"
    assert public_export["providers"][0]["test_model_id"] == "gpt-batch"
    assert public_export["providers"][0]["test_client_profile"] == CLIENT_PROFILE_CODEX
    assert public_export["providers"][0]["test_network_route"] == "proxy:Local Proxy"

    secret_response = client.post(
        "/export",
        data={"include_secrets": "on", "password": "long-test-password"},
    )
    secret_export = secret_response.json()
    assert secret_export["proxies"][0]["username"] == "user@name"
    assert secret_export["proxies"][0]["password"] == "p:a/ss"
    assert secret_export["providers"][0]["api_key"] == "sk-test-secret"

    reset_db()
    import_client = TestClient(app)
    import_client.post(
        "/setup",
        data={"password": "long-test-password", "confirm_password": "long-test-password"},
    )
    imported = import_client.post(
        "/import",
        files={"file": ("backup.json", json.dumps(secret_export).encode("utf-8"), "application/json")},
        follow_redirects=False,
    )
    assert imported.status_code == 303
    restored_export = import_client.post(
        "/export",
        data={"include_secrets": "on", "password": "long-test-password"},
    ).json()
    assert restored_export["proxies"][0]["username"] == "user@name"
    assert restored_export["proxies"][0]["password"] == "p:a/ss"
    with SessionLocal() as db:
        provider = db.scalar(select(Provider).where(Provider.name == "Proxy Relay"))
        assert provider is not None and provider.default_proxy is not None
        assert provider.default_proxy.name == "Local Proxy"
        assert provider.test_model_id == "gpt-batch"
        assert provider.test_client_profile == CLIENT_PROFILE_CODEX
        assert provider.test_network_route == f"proxy:{provider.default_proxy.id}"


def test_test_preferences_endpoint_saves_partial_updates_and_detail_selection() -> None:
    reset_db()
    client = TestClient(app)
    provider_id = setup_and_create_provider(client)

    model_response = client.post(
        f"/providers/{provider_id}/test-preferences",
        data={"model_id": "manual-batch-model"},
        headers={"accept": "application/json"},
    )
    profile_response = client.post(
        f"/providers/{provider_id}/test-preferences",
        data={"client_profile": CLIENT_PROFILE_CODEX},
        headers={"accept": "application/json"},
    )
    route_response = client.post(
        f"/providers/{provider_id}/test-preferences",
        data={"network_route": "direct"},
        headers={"accept": "application/json"},
    )

    assert model_response.status_code == profile_response.status_code == route_response.status_code == 200
    with SessionLocal() as db:
        provider = db.get(Provider, provider_id)
        assert provider is not None
        assert provider.test_model_id == "manual-batch-model"
        assert provider.test_client_profile == CLIENT_PROFILE_CODEX
        assert provider.test_network_route == "direct"
    detail = client.get(f"/providers/{provider_id}").text
    assert 'value="manual-batch-model"' in detail
    assert f'<option value="{CLIENT_PROFILE_CODEX}" selected>' in detail
    assert '<option value="direct" selected>' in detail
    assert "data-test-preferences" in detail
    assert "测试配置已保存" in detail


def test_saved_test_uses_persisted_preferences_and_returns_json(monkeypatch) -> None:
    reset_db()
    client = TestClient(app)
    provider_id = setup_and_create_provider(client)
    client.post(
        f"/providers/{provider_id}/test-preferences",
        data={"model_id": "saved-model", "client_profile": CLIENT_PROFILE_CLAUDE_CODE, "network_route": "direct"},
    )
    calls: list[tuple[str, str, str | None]] = []

    async def fake_test(base_url, api_key, model_id, client_profile, proxy_url=None):
        calls.append((model_id, client_profile, proxy_url))
        return ConnectivityTestResult("success", 17, "", '{"result":"pong"}')

    monkeypatch.setattr(main_module, "run_connectivity_test", fake_test)
    response = client.post(
        f"/providers/{provider_id}/test-saved",
        headers={"accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["model_id"] == "saved-model"
    assert response.json()["latency_ms"] == 17
    assert calls == [("saved-model", CLIENT_PROFILE_CLAUDE_CODE, None)]
    with SessionLocal() as db:
        test = db.scalar(select(ConnectivityTest).where(ConnectivityTest.provider_id == provider_id))
        assert test is not None and test.network_route == "直连"


def test_saved_test_records_missing_model_without_external_request(monkeypatch) -> None:
    reset_db()
    client = TestClient(app)
    provider_id = setup_and_create_provider(client)
    called = False

    async def should_not_call(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("不应发起外部请求")

    monkeypatch.setattr(main_module, "run_connectivity_test", should_not_call)
    response = client.post(
        f"/providers/{provider_id}/test-saved",
        headers={"accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert "未配置测试模型" in response.json()["error_message"]
    assert called is False
    with SessionLocal() as db:
        test = db.scalar(select(ConnectivityTest).where(ConnectivityTest.provider_id == provider_id))
        assert test is not None and test.model_id == "（未配置模型）"


def test_saved_test_skips_disabled_provider_without_writing_history(monkeypatch) -> None:
    reset_db()
    client = TestClient(app)
    provider_id = setup_and_create_provider(client)
    with SessionLocal() as db:
        provider = db.get(Provider, provider_id)
        assert provider is not None
        provider.enabled = False
        db.commit()

    response = client.post(
        f"/providers/{provider_id}/test-saved",
        headers={"accept": "application/json"},
    )

    assert response.status_code == 409
    assert response.json()["status"] == "skipped"
    with SessionLocal() as db:
        assert db.scalar(select(ConnectivityTest).where(ConnectivityTest.provider_id == provider_id)) is None


def test_index_exposes_test_all_and_eligible_provider_rows() -> None:
    reset_db()
    client = TestClient(app)
    enabled_id = setup_and_create_provider(client, "Enabled Relay")
    create = client.post(
        "/providers",
        data={
            "name": "Disabled Relay",
            "base_url": "https://disabled.example/v1",
            "api_key": "sk-disabled",
        },
        follow_redirects=False,
    )
    disabled_id = int(create.headers["location"].rsplit("/", 1)[-1])

    page = client.get("/").text

    assert "data-test-all" in page
    assert ">测试全部</button>" in page
    assert 'class="table-wrap home-table-wrap"' in page
    assert f'data-provider-id="{enabled_id}" data-provider-enabled="true"' in page
    assert f'data-provider-id="{disabled_id}" data-provider-enabled="false"' in page
    assert page.count("data-test-result") == 2


def test_proxy_delete_rejects_saved_test_route_reference() -> None:
    reset_db()
    client = TestClient(app)
    client.post("/setup", data={"password": "long-test-password", "confirm_password": "long-test-password"})
    proxy_id = create_proxy(client)
    provider_id = setup_and_create_provider(client, "Saved Route Relay")
    client.post(
        f"/providers/{provider_id}/test-preferences",
        data={"network_route": f"proxy:{proxy_id}"},
    )

    response = client.post(f"/proxies/{proxy_id}/delete", follow_redirects=True)

    assert "仍有 1 个中转站引用该代理" in response.text
    with SessionLocal() as db:
        assert db.get(NetworkProxy, proxy_id) is not None
