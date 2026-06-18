from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.sessions import SessionMiddleware

from .config import BASE_DIR, settings
from .db import get_db, init_db
from .models import ConnectivityTest, NetworkProxy, Provider, ProviderModel, utc_now
from .openai_compat import (
    CLIENT_PROFILE_LABELS,
    CLIENT_PROFILE_OPENAI_CHAT,
    VALID_CLIENT_PROFILES,
    client_profile_label,
    compact_json,
    fetch_models,
    normalize_base_url,
    run_connectivity_test,
)
from .security import (
    authenticate,
    check_session_password_proof,
    decrypt_api_key_with_fernet,
    decrypt_secret_with_fernet,
    encrypt_api_key_with_fernet,
    encrypt_secret_with_fernet,
    is_authenticated,
    is_initialized,
    key_hint,
    login_session,
    logout_session,
    require_session_fernet,
    setup_application,
)
from .proxy_support import (
    VALID_PROXY_SCHEMES,
    build_proxy_url,
    sanitize_proxy_error,
    test_proxy_connection,
    validate_proxy_fields,
)



@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    https_only=settings.cookie_secure,
    same_site="lax",
    max_age=60 * 60 * 12,
)

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def require_initialized(db: Session) -> None:
    if not is_initialized(db):
        raise HTTPException(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": "/setup"})


def current_user_required(request: Request, db: Annotated[Session, Depends(get_db)]) -> None:
    if not is_initialized(db):
        raise HTTPException(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": "/setup"})
    if not is_authenticated(request):
        if wants_json(request):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要先登录。")
        raise HTTPException(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": "/login"})


def render(request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    base_context = {
        "request": request,
        "app_name": settings.app_name,
        "authenticated": is_authenticated(request),
        "flash": request.session.pop("flash", None),
        "client_profile_labels": CLIENT_PROFILE_LABELS,
    }
    base_context.update(context)
    return templates.TemplateResponse(request, name, base_context, status_code=status_code)


def flash(request: Request, message: str, level: str = "info") -> None:
    request.session["flash"] = {"message": message, "level": level}


def provider_or_404(db: Session, provider_id: int) -> Provider:
    provider = db.scalar(
        select(Provider)
        .where(Provider.id == provider_id)
        .options(selectinload(Provider.models), selectinload(Provider.tests))
    )
    if provider is None:
        raise HTTPException(status_code=404, detail="中转站不存在。")
    return provider


def proxy_or_404(db: Session, proxy_id: int) -> NetworkProxy:
    proxy = db.get(NetworkProxy, proxy_id)
    if proxy is None:
        raise HTTPException(status_code=404, detail="网络代理不存在。")
    return proxy


def all_proxies(db: Session) -> list[NetworkProxy]:
    return list(db.scalars(select(NetworkProxy).order_by(NetworkProxy.name)).all())


def parse_default_proxy(
    db: Session,
    value: str,
    current_proxy_id: int | None = None,
) -> tuple[NetworkProxy | None, str | None]:
    if not value.strip():
        return None, None
    try:
        proxy_id = int(value)
    except ValueError:
        return None, "默认网络代理无效。"
    proxy = db.get(NetworkProxy, proxy_id)
    if proxy is None:
        return None, "默认网络代理不存在。"
    if not proxy.enabled and proxy.id != current_proxy_id:
        return None, "不能选择已禁用的网络代理。"
    return proxy, None


def resolve_network_route(
    request: Request,
    db: Session,
    provider: Provider,
    route: str,
) -> tuple[str | None, str]:
    selected = route.strip() or "default"
    if selected == "direct":
        return None, "直连"
    if selected == "default":
        proxy = provider.default_proxy
        if proxy is None:
            return None, "直连"
    elif selected.startswith("proxy:"):
        try:
            proxy = db.get(NetworkProxy, int(selected.removeprefix("proxy:")))
        except ValueError as exc:
            raise ValueError("网络路径无效。") from exc
        if proxy is None:
            raise ValueError("所选网络代理不存在。")
    else:
        raise ValueError("网络路径无效。")
    if not proxy.enabled:
        raise ValueError(f"网络代理“{proxy.name}”已禁用，请启用代理或临时选择直连。")
    fernet = require_session_fernet(request)
    return build_proxy_url(proxy, fernet), proxy.name


def serialize_provider(
    provider: Provider,
    include_secret: bool = False,
    api_key: str | None = None,
    test_network_route: str | None = None,
) -> dict[str, Any]:
    latest_test = provider.tests[0] if provider.tests else None
    payload: dict[str, Any] = {
        "name": provider.name,
        "base_url": provider.base_url,
        "notes": provider.notes,
        "enabled": provider.enabled,
        "client_profile": provider.client_profile,
        "test_model_id": provider.test_model_id,
        "test_client_profile": provider.test_client_profile,
        "test_network_route": test_network_route or provider.test_network_route or "default",
        "default_proxy": provider.default_proxy.name if provider.default_proxy else None,
        "archived_at": provider.archived_at.isoformat() if provider.archived_at else None,
        "key_hint": provider.key_hint,
        "models": [
            {
                "model_id": model.model_id,
                "owned_by": model.owned_by,
                "last_seen_at": model.last_seen_at.isoformat(),
                "raw_json": safe_json_loads(model.raw_json),
            }
            for model in provider.models
        ],
        "latest_test": None,
    }
    if latest_test:
        payload["latest_test"] = {
            "model_id": latest_test.model_id,
            "client_profile": latest_test.client_profile,
            "network_route": latest_test.network_route,
            "status": latest_test.status,
            "latency_ms": latest_test.latency_ms,
            "error_message": latest_test.error_message,
            "tested_at": latest_test.tested_at.isoformat(),
        }
    if include_secret:
        payload["api_key"] = api_key or ""
    return payload


def serialize_proxy(
    proxy: NetworkProxy,
    include_secret: bool = False,
    username: str = "",
    password: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": proxy.name,
        "scheme": proxy.scheme,
        "host": proxy.host,
        "port": proxy.port,
        "notes": proxy.notes,
        "enabled": proxy.enabled,
        "has_auth": proxy.has_auth,
    }
    if include_secret:
        payload["username"] = username
        payload["password"] = password
    return payload


def safe_json_loads(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def parse_archived_at(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("archived_at 必须是 ISO 8601 时间字符串或 null")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def reject_archived_provider(request: Request, provider: Provider) -> Response | None:
    if provider.archived_at is None:
        return None
    flash(request, "该中转站已归档，只能查看、恢复或永久删除。", "error")
    return redirect(f"/providers/{provider.id}")


def upsert_models(db: Session, provider: Provider, model_payloads: list[Any]) -> int:
    existing = {model.model_id: model for model in provider.models}
    seen = 0
    for item in model_payloads:
        if not isinstance(item, dict):
            continue
        model_id = item.get("model_id") or item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        seen += 1
        owned_by = item.get("owned_by") if isinstance(item.get("owned_by"), str) else ""
        raw = item.get("raw_json") if isinstance(item.get("raw_json"), dict) else item
        model = existing.get(model_id)
        if model is None:
            db.add(
                ProviderModel(
                    provider_id=provider.id,
                    model_id=model_id,
                    owned_by=owned_by,
                    raw_json=compact_json(raw),
                    last_seen_at=utc_now(),
                )
            )
        else:
            model.owned_by = owned_by
            model.raw_json = compact_json(raw)
            model.last_seen_at = utc_now()
    return seen


def get_default_model_id(provider: Provider) -> str | None:
    if not provider.models:
        return None

    model_ids = {model.model_id for model in provider.models}
    latest_test = provider.tests[0] if provider.tests else None
    if latest_test and latest_test.model_id in model_ids:
        return latest_test.model_id
    return provider.models[0].model_id


def get_test_preferences(provider: Provider) -> tuple[str | None, str, str]:
    return (
        provider.test_model_id or get_default_model_id(provider),
        provider.test_client_profile or provider.client_profile,
        provider.test_network_route or "default",
    )


def validate_test_network_route(
    db: Session,
    route: str,
    current_route: str | None = None,
) -> str | None:
    if route in {"default", "direct"}:
        return None
    if not route.startswith("proxy:"):
        return "网络路径无效。"
    try:
        proxy_id = int(route.removeprefix("proxy:"))
    except ValueError:
        return "网络路径无效。"
    proxy = db.get(NetworkProxy, proxy_id)
    if proxy is None:
        return "所选网络代理不存在。"
    if not proxy.enabled and route != current_route:
        return "不能选择已禁用的网络代理。"
    return None


def network_route_snapshot(db: Session, provider: Provider, route: str) -> str:
    if route == "direct":
        return "直连"
    if route == "default":
        return provider.default_proxy.name if provider.default_proxy else "直连"
    if route.startswith("proxy:"):
        try:
            proxy_id = int(route.removeprefix("proxy:"))
        except ValueError:
            return "无效网络路径"
        proxy = db.get(NetworkProxy, proxy_id)
        return proxy.name if proxy else f"已删除代理 #{proxy_id}"
    return "无效网络路径"


def serialize_test_network_route(db: Session, route: str) -> str:
    if not route.startswith("proxy:"):
        return route
    try:
        proxy_id = int(route.removeprefix("proxy:"))
    except ValueError:
        return route
    proxy = db.get(NetworkProxy, proxy_id)
    return f"proxy:{proxy.name}" if proxy else route


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Annotated[Session, Depends(get_db)]) -> Response:
    if not is_initialized(db):
        return render(request, "setup.html", {})
    if not is_authenticated(request):
        return render(request, "login.html", {})

    providers = db.scalars(
        select(Provider)
        .where(Provider.archived_at.is_(None))
        .options(selectinload(Provider.models), selectinload(Provider.tests))
        .order_by(Provider.name)
    ).all()
    return render(
        request,
        "index.html",
        {
            "providers": providers,
            "default_model_ids": {provider.id: get_default_model_id(provider) for provider in providers},
        },
    )


@app.get("/archive", response_class=HTMLResponse)
def archive_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    providers = db.scalars(
        select(Provider)
        .where(Provider.archived_at.is_not(None))
        .options(selectinload(Provider.models), selectinload(Provider.tests))
        .order_by(Provider.archived_at.desc(), Provider.name)
    ).all()
    return render(request, "archive.html", {"providers": providers})


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Annotated[Session, Depends(get_db)]) -> Response:
    if is_initialized(db):
        return redirect("/login")
    return render(request, "setup.html", {})


@app.post("/setup")
def setup_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
) -> Response:
    if is_initialized(db):
        return redirect("/login")
    if len(password) < 8:
        return render(request, "setup.html", {"error": "密码至少需要 8 个字符。"}, 400)
    if password != confirm_password:
        return render(request, "setup.html", {"error": "两次输入的密码不一致。"}, 400)

    setup_application(db, password)
    db.commit()
    login_session(request, db, password)
    flash(request, "应用已初始化。")
    return redirect("/")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Annotated[Session, Depends(get_db)]) -> Response:
    if not is_initialized(db):
        return redirect("/setup")
    if is_authenticated(request):
        return redirect("/")
    return render(request, "login.html", {})


@app.post("/login")
def login_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    password: Annotated[str, Form()],
) -> Response:
    if not is_initialized(db):
        return redirect("/setup")
    if not authenticate(db, password):
        return render(request, "login.html", {"error": "密码错误。"}, 401)
    db.commit()
    login_session(request, db, password)
    return redirect("/")


@app.post("/logout")
def logout(request: Request) -> Response:
    logout_session(request)
    return redirect("/login")


@app.get("/proxies", response_class=HTMLResponse)
def proxy_list_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    return render(request, "proxy_list.html", {"proxies": all_proxies(db)})


@app.get("/proxies/new", response_class=HTMLResponse)
def proxy_new_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    return render(request, "proxy_form.html", {"proxy": None, "mode": "new", "proxy_schemes": VALID_PROXY_SCHEMES})


@app.post("/proxies")
def proxy_create(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(current_user_required)] = None,
    name: Annotated[str, Form()] = "",
    scheme: Annotated[str, Form()] = "http",
    host: Annotated[str, Form()] = "",
    port: Annotated[str, Form()] = "",
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    enabled: Annotated[str | None, Form()] = None,
) -> Response:
    errors = validate_proxy_fields(name, scheme, host, port)
    if db.scalar(select(NetworkProxy).where(NetworkProxy.name == name.strip())):
        errors.append("代理名称已存在。")
    values = proxy_form_values(name, scheme, host, port, notes, enabled)
    if errors:
        return render(
            request,
            "proxy_form.html",
            {"proxy": None, "mode": "new", "proxy_schemes": VALID_PROXY_SCHEMES, "errors": errors, "form": values},
            400,
        )
    fernet = require_session_fernet(request)
    proxy = NetworkProxy(
        name=name.strip(),
        scheme=scheme,
        host=host.strip().strip("[]"),
        port=int(port),
        encrypted_username=encrypt_secret_with_fernet(username, fernet),
        encrypted_password=encrypt_secret_with_fernet(password, fernet),
        notes=notes.strip(),
        enabled=enabled == "on",
    )
    db.add(proxy)
    db.commit()
    flash(request, "网络代理已新增。")
    return redirect("/proxies")


@app.get("/proxies/{proxy_id}/edit", response_class=HTMLResponse)
def proxy_edit_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    proxy_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    return render(
        request,
        "proxy_form.html",
        {"proxy": proxy_or_404(db, proxy_id), "mode": "edit", "proxy_schemes": VALID_PROXY_SCHEMES},
    )


@app.post("/proxies/{proxy_id}")
def proxy_update(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    proxy_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
    name: Annotated[str, Form()] = "",
    scheme: Annotated[str, Form()] = "http",
    host: Annotated[str, Form()] = "",
    port: Annotated[str, Form()] = "",
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    enabled: Annotated[str | None, Form()] = None,
    clear_auth: Annotated[str | None, Form()] = None,
) -> Response:
    proxy = proxy_or_404(db, proxy_id)
    errors = validate_proxy_fields(name, scheme, host, port)
    duplicate = db.scalar(select(NetworkProxy).where(NetworkProxy.name == name.strip(), NetworkProxy.id != proxy.id))
    if duplicate:
        errors.append("代理名称已存在。")
    values = proxy_form_values(name, scheme, host, port, notes, enabled)
    if errors:
        return render(
            request,
            "proxy_form.html",
            {"proxy": proxy, "mode": "edit", "proxy_schemes": VALID_PROXY_SCHEMES, "errors": errors, "form": values},
            400,
        )
    proxy.name = name.strip()
    proxy.scheme = scheme
    proxy.host = host.strip().strip("[]")
    proxy.port = int(port)
    proxy.notes = notes.strip()
    proxy.enabled = enabled == "on"
    fernet = require_session_fernet(request)
    if clear_auth == "on":
        proxy.encrypted_username = ""
        proxy.encrypted_password = ""
    else:
        if username:
            proxy.encrypted_username = encrypt_secret_with_fernet(username, fernet)
        if password:
            proxy.encrypted_password = encrypt_secret_with_fernet(password, fernet)
    db.commit()
    flash(request, "网络代理已更新。")
    return redirect("/proxies")


@app.post("/proxies/{proxy_id}/toggle")
def proxy_toggle(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    proxy_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    proxy = proxy_or_404(db, proxy_id)
    proxy.enabled = not proxy.enabled
    db.commit()
    flash(request, f"网络代理已{'启用' if proxy.enabled else '禁用'}。")
    return redirect("/proxies")


@app.post("/proxies/{proxy_id}/delete")
def proxy_delete(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    proxy_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    proxy = proxy_or_404(db, proxy_id)
    default_references = set(db.scalars(select(Provider.id).where(Provider.default_proxy_id == proxy.id)).all())
    test_references = set(
        db.scalars(select(Provider.id).where(Provider.test_network_route == f"proxy:{proxy.id}")).all()
    )
    references = len(default_references | test_references)
    if references:
        flash(request, f"无法删除：仍有 {references} 个中转站引用该代理。", "error")
        return redirect("/proxies")
    db.delete(proxy)
    db.commit()
    flash(request, "网络代理已删除。")
    return redirect("/proxies")


@app.post("/proxies/{proxy_id}/test")
async def proxy_test(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    proxy_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    proxy = proxy_or_404(db, proxy_id)
    try:
        proxy_url = build_proxy_url(proxy, require_session_fernet(request))
        result = await test_proxy_connection(proxy_url)
    except Exception as exc:
        result = None
        proxy.last_test_status = "failed"
        proxy.last_exit_ip = ""
        proxy.last_latency_ms = None
        proxy.last_error = sanitize_proxy_error(exc)
    else:
        proxy.last_test_status = result.status
        proxy.last_exit_ip = result.exit_ip
        proxy.last_latency_ms = result.latency_ms
        proxy.last_error = result.error_message
    proxy.last_tested_at = utc_now()
    db.commit()
    if proxy.last_test_status == "success":
        flash(request, f"代理测试成功，出口 IP：{proxy.last_exit_ip}，耗时 {proxy.last_latency_ms} ms。")
    else:
        flash(request, f"代理测试失败：{proxy.last_error}", "error")
    return redirect("/proxies")


@app.get("/providers/new", response_class=HTMLResponse)
def provider_new(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    return render(request, "provider_form.html", {"provider": None, "mode": "new", "proxies": all_proxies(db)})


@app.post("/providers")
def provider_create(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(current_user_required)] = None,
    name: Annotated[str, Form()] = "",
    base_url: Annotated[str, Form()] = "",
    api_key: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    enabled: Annotated[str | None, Form()] = None,
    client_profile: Annotated[str, Form()] = CLIENT_PROFILE_OPENAI_CHAT,
    default_proxy_id: Annotated[str, Form()] = "",
) -> Response:
    errors = validate_provider_form(name, base_url, api_key, client_profile, require_key=True)
    default_proxy, proxy_error = parse_default_proxy(db, default_proxy_id)
    if proxy_error:
        errors.append(proxy_error)
    if errors:
        return render(
            request,
            "provider_form.html",
            {
                "provider": None,
                "mode": "new",
                "errors": errors,
                "form": form_values(name, base_url, notes, enabled, client_profile, default_proxy_id),
                "proxies": all_proxies(db),
            },
            400,
        )

    fernet = require_session_fernet(request)
    provider = Provider(
        name=name.strip(),
        base_url=normalize_base_url(base_url),
        encrypted_api_key=encrypt_api_key_with_fernet(api_key.strip(), fernet),
        key_hint=key_hint(api_key),
        notes=notes.strip(),
        enabled=enabled == "on",
        client_profile=client_profile,
        test_client_profile=client_profile,
        test_network_route="default",
        default_proxy_id=default_proxy.id if default_proxy else None,
    )
    db.add(provider)
    try:
        db.commit()
    except Exception:
        db.rollback()
        return render(
            request,
            "provider_form.html",
            {
                "provider": None,
                "mode": "new",
                "errors": ["已存在名称和 Base URL 相同的中转站。"],
                "form": form_values(name, base_url, notes, enabled, client_profile, default_proxy_id),
                "proxies": all_proxies(db),
            },
            400,
        )
    flash(request, "中转站已新增。")
    return redirect(f"/providers/{provider.id}")


@app.get("/providers/{provider_id}", response_class=HTMLResponse)
def provider_detail(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    provider = provider_or_404(db, provider_id)
    test_model_id, test_client_profile, test_network_route = get_test_preferences(provider)
    return render(
        request,
        "provider_detail.html",
        {
            "provider": provider,
            "default_model_id": test_model_id,
            "test_client_profile": test_client_profile,
            "test_network_route": test_network_route,
            "proxies": all_proxies(db),
        },
    )


@app.get("/providers/{provider_id}/api-key")
def provider_api_key(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> JSONResponse:
    provider = provider_or_404(db, provider_id)
    fernet = require_session_fernet(request)
    api_key = decrypt_api_key_with_fernet(provider.encrypted_api_key, fernet)
    return JSONResponse({"api_key": api_key})


@app.get("/providers/{provider_id}/edit", response_class=HTMLResponse)
def provider_edit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    provider = provider_or_404(db, provider_id)
    if blocked := reject_archived_provider(request, provider):
        return blocked
    return render(
        request,
        "provider_form.html",
        {"provider": provider, "mode": "edit", "proxies": all_proxies(db)},
    )


@app.post("/providers/{provider_id}")
def provider_update(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
    name: Annotated[str, Form()] = "",
    base_url: Annotated[str, Form()] = "",
    api_key: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    enabled: Annotated[str | None, Form()] = None,
    client_profile: Annotated[str, Form()] = CLIENT_PROFILE_OPENAI_CHAT,
    default_proxy_id: Annotated[str, Form()] = "",
) -> Response:
    provider = provider_or_404(db, provider_id)
    if blocked := reject_archived_provider(request, provider):
        return blocked
    errors = validate_provider_form(name, base_url, api_key, client_profile, require_key=False)
    default_proxy, proxy_error = parse_default_proxy(db, default_proxy_id, provider.default_proxy_id)
    if proxy_error:
        errors.append(proxy_error)
    if errors:
        return render(
            request,
            "provider_form.html",
            {
                "provider": provider,
                "mode": "edit",
                "errors": errors,
                "form": form_values(name, base_url, notes, enabled, client_profile, default_proxy_id),
                "proxies": all_proxies(db),
            },
            400,
        )

    provider.name = name.strip()
    provider.base_url = normalize_base_url(base_url)
    provider.notes = notes.strip()
    provider.enabled = enabled == "on"
    provider.client_profile = client_profile
    provider.default_proxy_id = default_proxy.id if default_proxy else None
    if api_key.strip():
        fernet = require_session_fernet(request)
        provider.encrypted_api_key = encrypt_api_key_with_fernet(api_key.strip(), fernet)
        provider.key_hint = key_hint(api_key)

    try:
        db.commit()
    except Exception:
        db.rollback()
        return render(
            request,
            "provider_form.html",
            {
                "provider": provider,
                "mode": "edit",
                "errors": ["已存在名称和 Base URL 相同的中转站。"],
                "form": form_values(name, base_url, notes, enabled, client_profile, default_proxy_id),
                "proxies": all_proxies(db),
            },
            400,
        )
    flash(request, "中转站已更新。")
    return redirect(f"/providers/{provider.id}")


@app.post("/providers/{provider_id}/archive")
def provider_archive(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    provider = provider_or_404(db, provider_id)
    if provider.archived_at is not None:
        flash(request, "该中转站已经在归档中。")
        return redirect("/archive")
    provider.archived_at = utc_now()
    db.commit()
    flash(request, f"“{provider.name}”已归档。")
    return redirect("/")


@app.post("/providers/{provider_id}/restore")
def provider_restore(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    provider = provider_or_404(db, provider_id)
    if provider.archived_at is None:
        flash(request, "该中转站当前未归档。")
        return redirect(f"/providers/{provider.id}")
    provider.archived_at = None
    db.commit()
    flash(request, f"“{provider.name}”已恢复。")
    return redirect("/archive")


@app.post("/providers/{provider_id}/delete")
def provider_delete(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    provider = provider_or_404(db, provider_id)
    was_archived = provider.archived_at is not None
    provider_name = provider.name
    db.delete(provider)
    db.commit()
    flash(request, f"“{provider_name}”已永久删除。" if was_archived else "中转站已删除。")
    return redirect("/archive" if was_archived else "/")


@app.post("/providers/{provider_id}/refresh-models")
async def provider_refresh_models(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    provider = provider_or_404(db, provider_id)
    if blocked := reject_archived_provider(request, provider):
        return blocked
    fernet = require_session_fernet(request)
    proxy_url: str | None = None
    try:
        proxy_url, network_route = resolve_network_route(request, db, provider, "default")
        api_key = decrypt_api_key_with_fernet(provider.encrypted_api_key, fernet)
        models = await fetch_models(provider.base_url, api_key, provider.client_profile, proxy_url=proxy_url)
    except httpx.HTTPStatusError as exc:
        flash(request, f"刷新模型失败：HTTP {exc.response.status_code} {exc.response.text[:300]}", "error")
        return redirect(f"/providers/{provider.id}")
    except Exception as exc:
        detail = sanitize_proxy_error(exc) if proxy_url else str(exc)
        flash(request, f"刷新模型失败：{detail}", "error")
        return redirect(f"/providers/{provider.id}")

    existing = {model.model_id: model for model in provider.models}
    now = utc_now()
    for model_info in models:
        model = existing.get(model_info.model_id)
        if model is None:
            db.add(
                ProviderModel(
                    provider_id=provider.id,
                    model_id=model_info.model_id,
                    owned_by=model_info.owned_by,
                    raw_json=compact_json(model_info.raw_json),
                    last_seen_at=now,
                )
            )
        else:
            model.owned_by = model_info.owned_by
            model.raw_json = compact_json(model_info.raw_json)
            model.last_seen_at = now
    db.commit()
    flash(
        request,
        f"已使用{client_profile_label(provider.client_profile)}模式通过{network_route}获取 {len(models)} 个模型。",
    )
    return redirect(f"/providers/{provider.id}")


def test_result_payload(test: ConnectivityTest) -> dict[str, Any]:
    return {
        "status": test.status,
        "model_id": test.model_id,
        "latency_ms": test.latency_ms,
        "error_message": test.error_message,
        "tested_at": test.tested_at.isoformat(),
    }


async def execute_provider_test(
    request: Request,
    db: Session,
    provider: Provider,
    model_id: str | None,
    client_profile: str,
    network_route: str,
) -> ConnectivityTest:
    clean_model_id = (model_id or "").strip()
    stored_model_id = clean_model_id or "（未配置模型）"
    stored_profile = client_profile if client_profile in VALID_CLIENT_PROFILES else provider.client_profile
    route_label = network_route_snapshot(db, provider, network_route)
    preflight_error = ""
    if not clean_model_id:
        preflight_error = "未配置测试模型。"
    elif len(clean_model_id) > 260:
        preflight_error = "模型名称不能超过 260 个字符。"
    elif client_profile not in VALID_CLIENT_PROFILES:
        preflight_error = "客户端模式无效。"

    proxy_url: str | None = None
    if not preflight_error:
        try:
            proxy_url, route_label = resolve_network_route(request, db, provider, network_route)
        except Exception as exc:
            preflight_error = sanitize_proxy_error(exc) if proxy_url else str(exc)

    if preflight_error:
        test = ConnectivityTest(
            provider_id=provider.id,
            model_id=stored_model_id,
            client_profile=stored_profile,
            status="failed",
            latency_ms=None,
            error_message=preflight_error,
            raw_response_excerpt="",
            network_route=route_label,
            tested_at=utc_now(),
        )
    else:
        try:
            fernet = require_session_fernet(request)
            api_key = decrypt_api_key_with_fernet(provider.encrypted_api_key, fernet)
            result = await run_connectivity_test(
                provider.base_url,
                api_key,
                clean_model_id,
                client_profile,
                proxy_url=proxy_url,
            )
            test = ConnectivityTest(
                provider_id=provider.id,
                model_id=clean_model_id,
                client_profile=client_profile,
                status=result.status,
                latency_ms=result.latency_ms,
                error_message=result.error_message,
                raw_response_excerpt=result.raw_response_excerpt,
                network_route=route_label,
                tested_at=utc_now(),
            )
        except Exception as exc:
            detail = sanitize_proxy_error(exc) if proxy_url else str(exc)
            test = ConnectivityTest(
                provider_id=provider.id,
                model_id=clean_model_id,
                client_profile=client_profile,
                status="failed",
                latency_ms=None,
                error_message=detail,
                raw_response_excerpt="",
                network_route=route_label,
                tested_at=utc_now(),
            )

    db.add(test)
    db.commit()
    db.refresh(test)
    return test


@app.post("/providers/{provider_id}/test-preferences")
def provider_test_preferences(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
    model_id: Annotated[str | None, Form()] = None,
    client_profile: Annotated[str | None, Form()] = None,
    network_route: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    provider = provider_or_404(db, provider_id)
    if provider.archived_at is not None:
        return JSONResponse({"error": "已归档中转站不能修改测试配置。"}, status_code=409)
    if model_id is None and client_profile is None and network_route is None:
        return JSONResponse({"error": "没有需要保存的测试配置。"}, status_code=400)
    if model_id is not None:
        clean_model_id = model_id.strip()
        if len(clean_model_id) > 260:
            return JSONResponse({"error": "模型名称不能超过 260 个字符。"}, status_code=400)
        provider.test_model_id = clean_model_id or None
    if client_profile is not None:
        if client_profile not in VALID_CLIENT_PROFILES:
            return JSONResponse({"error": "客户端模式无效。"}, status_code=400)
        provider.test_client_profile = client_profile
    if network_route is not None:
        route_error = validate_test_network_route(db, network_route, provider.test_network_route)
        if route_error:
            return JSONResponse({"error": route_error}, status_code=400)
        provider.test_network_route = network_route
    db.commit()
    saved_model, saved_profile, saved_route = get_test_preferences(provider)
    return JSONResponse(
        {
            "ok": True,
            "model_id": saved_model,
            "client_profile": saved_profile,
            "network_route": saved_route,
        }
    )


@app.post("/providers/{provider_id}/test-saved")
async def provider_test_saved(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> JSONResponse:
    provider = provider_or_404(db, provider_id)
    if provider.archived_at is not None or not provider.enabled:
        return JSONResponse({"status": "skipped", "error_message": "中转站已禁用或归档。"}, status_code=409)
    model_id, client_profile, network_route = get_test_preferences(provider)
    test = await execute_provider_test(request, db, provider, model_id, client_profile, network_route)
    return JSONResponse(test_result_payload(test))


@app.post("/providers/{provider_id}/test")
async def provider_test(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
    model_id: Annotated[str, Form()] = "",
    client_profile: Annotated[str, Form()] = "",
    network_route: Annotated[str, Form()] = "default",
) -> Response:
    provider = provider_or_404(db, provider_id)
    if blocked := reject_archived_provider(request, provider):
        return blocked
    model_id = model_id.strip()
    if not model_id:
        flash(request, "请输入要测试的模型。", "error")
        return redirect(f"/providers/{provider.id}")
    if len(model_id) > 260:
        flash(request, "模型名称不能超过 260 个字符。", "error")
        return redirect(f"/providers/{provider.id}")
    test_profile = client_profile.strip() or provider.client_profile
    if test_profile not in VALID_CLIENT_PROFILES:
        flash(request, "客户端模式无效，未执行测试。", "error")
        return redirect(f"/providers/{provider.id}")

    route_error = validate_test_network_route(db, network_route, provider.test_network_route)
    if route_error:
        flash(request, route_error, "error")
        return redirect(f"/providers/{provider.id}")
    provider.test_model_id = model_id
    provider.test_client_profile = test_profile
    provider.test_network_route = network_route
    db.commit()
    test = await execute_provider_test(request, db, provider, model_id, test_profile, network_route)
    profile_name = client_profile_label(test_profile)
    if test.status == "success":
        flash(request, f"{profile_name}连通性测试成功，耗时 {test.latency_ms} ms。")
    else:
        flash(request, f"{profile_name}连通性测试失败：{test.error_message}", "error")
    return redirect(f"/providers/{provider.id}")


@app.get("/import-export", response_class=HTMLResponse)
def import_export_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    return render(request, "import_export.html", {})


@app.post("/export")
def export_json(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(current_user_required)] = None,
    include_secrets: Annotated[str | None, Form()] = None,
    password: Annotated[str, Form()] = "",
) -> Response:
    include_secret_values = include_secrets == "on"
    fernet = require_session_fernet(request)
    if include_secret_values:
        if not password or not authenticate(db, password) or not check_session_password_proof(request, db, password):
            flash(request, "密码确认失败，未导出明文 API Key 和代理认证信息。", "error")
            return redirect("/import-export")

    providers = db.scalars(
        select(Provider)
        .options(selectinload(Provider.models), selectinload(Provider.tests), selectinload(Provider.default_proxy))
        .order_by(Provider.name)
    ).all()
    payload = {
        "version": 3,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "contains_secrets": include_secret_values,
        "proxies": [],
        "providers": [],
    }
    for proxy in all_proxies(db):
        username = decrypt_secret_with_fernet(proxy.encrypted_username, fernet) if include_secret_values else ""
        proxy_password = decrypt_secret_with_fernet(proxy.encrypted_password, fernet) if include_secret_values else ""
        payload["proxies"].append(serialize_proxy(proxy, include_secret_values, username, proxy_password))
    for provider in providers:
        api_key = decrypt_api_key_with_fernet(provider.encrypted_api_key, fernet) if include_secret_values else None
        exported_route = serialize_test_network_route(db, provider.test_network_route or "default")
        payload["providers"].append(serialize_provider(provider, include_secret_values, api_key, exported_route))

    content = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        content,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="keys-export.json"'},
    )


@app.post("/import")
async def import_json(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File()],
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    raw = await file.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        flash(request, f"导入失败：JSON 格式无效（{exc}）。", "error")
        return redirect("/import-export")

    providers = payload.get("providers") if isinstance(payload, dict) else None
    proxies = payload.get("proxies", []) if isinstance(payload, dict) else None
    if not isinstance(providers, list):
        flash(request, "导入失败：JSON 必须包含 providers 数组。", "error")
        return redirect("/import-export")
    if not isinstance(proxies, list):
        flash(request, "导入失败：proxies 必须是数组。", "error")
        return redirect("/import-export")

    fernet = require_session_fernet(request)
    created = 0
    updated = 0
    proxy_created = 0
    proxy_updated = 0
    errors: list[str] = []
    for index, item in enumerate(proxies, start=1):
        if not isinstance(item, dict):
            errors.append(f"代理第 {index} 条：代理必须是对象。")
            continue
        name = str(item.get("name") or "").strip()
        scheme = str(item.get("scheme") or "").strip().lower()
        host = str(item.get("host") or "").strip().strip("[]")
        port = item.get("port", "")
        proxy_errors = validate_proxy_fields(name, scheme, host, port)
        if proxy_errors:
            errors.append(f"代理第 {index} 条：{' '.join(proxy_errors)}")
            continue

        proxy = db.scalar(select(NetworkProxy).where(NetworkProxy.name == name))
        if proxy is None:
            proxy = NetworkProxy(name=name, scheme=scheme, host=host, port=int(port))
            db.add(proxy)
            proxy_created += 1
        else:
            proxy.scheme = scheme
            proxy.host = host
            proxy.port = int(port)
            proxy_updated += 1
        proxy.notes = str(item.get("notes") or "").strip()
        proxy.enabled = bool(item.get("enabled", True))
        if "username" in item:
            proxy.encrypted_username = encrypt_secret_with_fernet(str(item.get("username") or ""), fernet)
        if "password" in item:
            proxy.encrypted_password = encrypt_secret_with_fernet(str(item.get("password") or ""), fernet)
        db.flush()

    for index, item in enumerate(providers, start=1):
        if not isinstance(item, dict):
            errors.append(f"第 {index} 条：中转站必须是对象。")
            continue
        name = str(item.get("name") or "").strip()
        base_url = normalize_base_url(str(item.get("base_url") or ""))
        notes = str(item.get("notes") or "").strip()
        enabled = bool(item.get("enabled", True))
        api_key = str(item.get("api_key") or "").strip()
        client_profile = str(item.get("client_profile") or CLIENT_PROFILE_OPENAI_CHAT).strip()
        if client_profile not in VALID_CLIENT_PROFILES:
            errors.append(f"第 {index} 条：client_profile 无效。")
            continue
        try:
            archived_at = parse_archived_at(item.get("archived_at"))
        except (TypeError, ValueError) as exc:
            errors.append(f"第 {index} 条：archived_at 无效（{exc}）。")
            continue
        if not name or not base_url:
            errors.append(f"第 {index} 条：name 和 base_url 必填。")
            continue
        test_model_value = item.get("test_model_id")
        test_model_id = str(test_model_value).strip() if test_model_value not in (None, "") else None
        if test_model_id and len(test_model_id) > 260:
            errors.append(f"第 {index} 条：test_model_id 不能超过 260 个字符。")
            continue
        test_profile_value = item.get("test_client_profile")
        test_client_profile = str(test_profile_value).strip() if test_profile_value not in (None, "") else None
        if test_client_profile is not None and test_client_profile not in VALID_CLIENT_PROFILES:
            errors.append(f"第 {index} 条：test_client_profile 无效。")
            continue
        imported_test_route = str(item.get("test_network_route") or "default").strip()
        if imported_test_route in {"default", "direct"}:
            test_network_route = imported_test_route
        elif imported_test_route.startswith("proxy:"):
            proxy_name = imported_test_route.removeprefix("proxy:").strip()
            test_proxy = db.scalar(select(NetworkProxy).where(NetworkProxy.name == proxy_name))
            if test_proxy is None:
                errors.append(f"第 {index} 条：找不到测试网络代理“{proxy_name}”。")
                continue
            test_network_route = f"proxy:{test_proxy.id}"
        else:
            errors.append(f"第 {index} 条：test_network_route 无效。")
            continue
        default_proxy_name = item.get("default_proxy")
        default_proxy: NetworkProxy | None = None
        if default_proxy_name not in (None, ""):
            if not isinstance(default_proxy_name, str):
                errors.append(f"第 {index} 条：default_proxy 必须是代理名称或 null。")
                continue
            default_proxy = db.scalar(select(NetworkProxy).where(NetworkProxy.name == default_proxy_name.strip()))
            if default_proxy is None:
                errors.append(f"第 {index} 条：找不到默认代理“{default_proxy_name}”。")
                continue

        provider = db.scalar(select(Provider).where(Provider.name == name, Provider.base_url == base_url))
        if provider is None:
            if not api_key:
                errors.append(f"第 {index} 条：新增中转站 {name} 必须提供 api_key。")
                continue
            provider = Provider(
                name=name,
                base_url=base_url,
                encrypted_api_key=encrypt_api_key_with_fernet(api_key, fernet),
                key_hint=key_hint(api_key),
                notes=notes,
                enabled=enabled,
                client_profile=client_profile,
                test_model_id=test_model_id,
                test_client_profile=test_client_profile,
                test_network_route=test_network_route,
                default_proxy_id=default_proxy.id if default_proxy else None,
                archived_at=archived_at,
            )
            db.add(provider)
            db.flush()
            created += 1
        else:
            provider.notes = notes
            provider.enabled = enabled
            provider.client_profile = client_profile
            provider.test_model_id = test_model_id
            provider.test_client_profile = test_client_profile
            provider.test_network_route = test_network_route
            provider.default_proxy_id = default_proxy.id if default_proxy else None
            provider.archived_at = archived_at
            if api_key:
                provider.encrypted_api_key = encrypt_api_key_with_fernet(api_key, fernet)
                provider.key_hint = key_hint(api_key)
            updated += 1

        model_count = upsert_models(db, provider, item.get("models") if isinstance(item.get("models"), list) else [])
        if model_count:
            provider.updated_at = utc_now()

    db.commit()
    message = (
        f"导入完成：代理新增 {proxy_created} 个、更新 {proxy_updated} 个；"
        f"中转站新增 {created} 个、更新 {updated} 个"
    )
    if errors:
        message += f"，{len(errors)} 个错误。" + " ".join(errors[:5])
        flash(request, message, "error")
    else:
        flash(request, message + "。")
    return redirect("/import-export")


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


def validate_provider_form(
    name: str,
    base_url: str,
    api_key: str,
    client_profile: str,
    require_key: bool,
) -> list[str]:
    errors: list[str] = []
    if not name.strip():
        errors.append("名称必填。")
    normalized = normalize_base_url(base_url)
    if not normalized:
        errors.append("Base URL 必填。")
    elif not (normalized.startswith("http://") or normalized.startswith("https://")):
        errors.append("Base URL 必须以 http:// 或 https:// 开头。")
    if require_key and not api_key.strip():
        errors.append("API Key 必填。")
    if client_profile not in VALID_CLIENT_PROFILES:
        errors.append("客户端模式无效。")
    return errors


def form_values(
    name: str,
    base_url: str,
    notes: str,
    enabled: str | None,
    client_profile: str,
    default_proxy_id: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "base_url": base_url,
        "notes": notes,
        "enabled": enabled == "on",
        "client_profile": client_profile,
        "default_proxy_id": default_proxy_id,
    }


def proxy_form_values(
    name: str,
    scheme: str,
    host: str,
    port: str,
    notes: str,
    enabled: str | None,
) -> dict[str, Any]:
    return {
        "name": name,
        "scheme": scheme,
        "host": host,
        "port": port,
        "notes": notes,
        "enabled": enabled == "on",
    }
