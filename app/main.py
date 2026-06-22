from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.sessions import SessionMiddleware

from .config import BASE_DIR, settings
from .analytics import (
    VALID_RANGES,
    VALID_SOURCES,
    build_statistics,
    load_real_records,
)
from .db import get_db, init_db
from .models import (
    ConnectivityTest,
    NetworkProxy,
    Provider,
    ProviderGroup,
    ProviderModel,
    ScheduledRun,
    ScheduledTask,
    ScheduledTaskProvider,
    utc_now,
)
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
    authorize_scheduler_vault,
    check_session_password_proof,
    clear_scheduler_vault,
    decrypt_api_key_with_fernet,
    decrypt_secret_with_fernet,
    encrypt_api_key_with_fernet,
    encrypt_secret_with_fernet,
    get_scheduler_fernet,
    get_setting,
    is_authenticated,
    is_initialized,
    key_hint,
    login_session,
    logout_session,
    require_session_fernet,
    setup_application,
    set_setting,
)
from .proxy_support import (
    VALID_PROXY_SCHEMES,
    build_proxy_url,
    sanitize_proxy_error,
    test_proxy_connection,
    validate_proxy_fields,
)
from .scheduler import (
    calculate_next_run,
    enqueue_task_run,
    scheduler_vault_state,
    set_task_provider_links,
    start_scheduler,
    stop_scheduler,
    task_is_running,
    task_schedule_summary,
    task_target_summary,
    validate_schedule_values,
)
from .test_runner import (
    network_route_snapshot,
    resolve_network_route_with_fernet,
    run_provider_connectivity_test,
)


SETTING_STATISTICS_FILTER_PREFERENCES = "statistics_filter_preferences"
DEFAULT_STATISTICS_FILTERS = {"range": "7d", "provider_id": "all", "source": "all"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    await start_scheduler()
    try:
        yield
    finally:
        await stop_scheduler()


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
        "format_local_datetime": format_local_datetime,
    }
    base_context.update(context)
    return templates.TemplateResponse(request, name, base_context, status_code=status_code)


def format_local_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(settings.app_timezone)).strftime("%Y-%m-%d %H:%M:%S")


def read_statistics_filter_preferences(db: Session) -> tuple[dict[str, str], bool]:
    stored = get_setting(db, SETTING_STATISTICS_FILTER_PREFERENCES)
    if not stored:
        return DEFAULT_STATISTICS_FILTERS.copy(), False
    try:
        payload = json.loads(stored)
    except json.JSONDecodeError:
        return DEFAULT_STATISTICS_FILTERS.copy(), True
    if not isinstance(payload, dict):
        return DEFAULT_STATISTICS_FILTERS.copy(), True
    return {
        "range": str(payload.get("range", DEFAULT_STATISTICS_FILTERS["range"])),
        "provider_id": str(payload.get("provider_id", DEFAULT_STATISTICS_FILTERS["provider_id"])),
        "source": str(payload.get("source", DEFAULT_STATISTICS_FILTERS["source"])),
    }, True


def write_statistics_filter_preferences(db: Session, filters: dict[str, str]) -> None:
    set_setting(
        db,
        SETTING_STATISTICS_FILTER_PREFERENCES,
        json.dumps(filters, ensure_ascii=False, sort_keys=True),
    )


def normalize_statistics_filters(
    *,
    range_key: str,
    provider_id: str,
    source: str,
    valid_provider_ids: set[int],
) -> tuple[str, str, str, int | None]:
    if range_key not in VALID_RANGES:
        range_key = DEFAULT_STATISTICS_FILTERS["range"]
    if source not in VALID_SOURCES:
        source = DEFAULT_STATISTICS_FILTERS["source"]
    selected_provider_id: int | None = None
    if provider_id != "all":
        try:
            selected_provider_id = int(provider_id)
        except ValueError:
            provider_id = DEFAULT_STATISTICS_FILTERS["provider_id"]
    if selected_provider_id not in valid_provider_ids:
        selected_provider_id = None
        provider_id = DEFAULT_STATISTICS_FILTERS["provider_id"]
    return range_key, provider_id, source, selected_provider_id


def flash(request: Request, message: str, level: str = "info") -> None:
    request.session["flash"] = {"message": message, "level": level}


def provider_or_404(db: Session, provider_id: int) -> Provider:
    provider = db.scalar(
        select(Provider)
        .where(Provider.id == provider_id)
        .options(selectinload(Provider.models), selectinload(Provider.tests), selectinload(Provider.group))
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


def all_provider_groups(db: Session) -> list[ProviderGroup]:
    return list(db.scalars(select(ProviderGroup).order_by(ProviderGroup.created_at, ProviderGroup.id)).all())


def normalize_group_name(value: str) -> str:
    return " ".join(value.split())


def validate_group_name(value: str) -> tuple[str, str | None]:
    cleaned = normalize_group_name(value)
    if len(cleaned) > 100:
        return cleaned, "分组名称不能超过 100 个字符。"
    return cleaned, None


def resolve_provider_group(db: Session, group_name: str) -> ProviderGroup | None:
    if not group_name:
        return None
    normalized_name = group_name.casefold()
    group = db.scalar(select(ProviderGroup).where(ProviderGroup.normalized_name == normalized_name))
    if group is not None:
        return group
    group = ProviderGroup(name=group_name, normalized_name=normalized_name)
    db.add(group)
    db.flush()
    return group


def delete_group_if_empty(db: Session, group_id: int | None) -> None:
    if group_id is None:
        return
    db.flush()
    if db.scalar(select(Provider.id).where(Provider.group_id == group_id).limit(1)) is not None:
        return
    group = db.get(ProviderGroup, group_id)
    if group is not None:
        db.execute(update(ScheduledTask).where(ScheduledTask.group_id == group_id).values(group_id=None))
        db.delete(group)


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
    return resolve_network_route_with_fernet(
        db, provider, route, require_session_fernet(request)
    )


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
        "group": provider.group.name if provider.group else None,
        "archived_at": provider.archived_at.isoformat() if provider.archived_at else None,
        "key_hint": provider.key_hint,
        "models": [
            {
                "model_id": model.model_id,
                "is_manual": model.is_manual,
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
        is_manual = item.get("is_manual") is True
        raw = item.get("raw_json") if isinstance(item.get("raw_json"), dict) else item
        model = existing.get(model_id)
        if model is None:
            db.add(
                ProviderModel(
                    provider_id=provider.id,
                    model_id=model_id,
                    is_manual=is_manual,
                    owned_by=owned_by,
                    raw_json=compact_json(raw),
                    last_seen_at=utc_now(),
                )
            )
        else:
            model.is_manual = is_manual
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


def select_next_test_model(db: Session, provider: Provider, removed_model_ids: set[str]) -> None:
    if not provider.test_model_id or provider.test_model_id not in removed_model_ids:
        return
    db.flush()
    provider.test_model_id = db.scalar(
        select(ProviderModel.model_id)
        .where(ProviderModel.provider_id == provider.id)
        .order_by(ProviderModel.is_manual.desc(), ProviderModel.model_id.asc())
        .limit(1)
    )


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
        .options(selectinload(Provider.models), selectinload(Provider.tests), selectinload(Provider.group))
        .order_by(Provider.name)
    ).all()
    ungrouped_providers = [provider for provider in providers if provider.group_id is None]
    providers_by_group: dict[int, list[Provider]] = {}
    for provider in providers:
        if provider.group_id is not None:
            providers_by_group.setdefault(provider.group_id, []).append(provider)
    group_sections = [
        (group, providers_by_group[group.id])
        for group in all_provider_groups(db)
        if group.id in providers_by_group
    ]
    return render(
        request,
        "index.html",
        {
            "providers": providers,
            "ungrouped_providers": ungrouped_providers,
            "group_sections": group_sections,
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
    return render(
        request,
        "provider_form.html",
        {"provider": None, "mode": "new", "proxies": all_proxies(db), "groups": all_provider_groups(db)},
    )


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
    group_name: Annotated[str, Form()] = "",
) -> Response:
    errors = validate_provider_form(name, base_url, api_key, client_profile, require_key=True)
    clean_group_name, group_error = validate_group_name(group_name)
    if group_error:
        errors.append(group_error)
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
                "form": form_values(
                    name, base_url, notes, enabled, client_profile, default_proxy_id, clean_group_name
                ),
                "proxies": all_proxies(db),
                "groups": all_provider_groups(db),
            },
            400,
        )

    fernet = require_session_fernet(request)
    group = resolve_provider_group(db, clean_group_name)
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
        group_id=group.id if group else None,
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
                "form": form_values(
                    name, base_url, notes, enabled, client_profile, default_proxy_id, clean_group_name
                ),
                "proxies": all_proxies(db),
                "groups": all_provider_groups(db),
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
        {
            "provider": provider,
            "mode": "edit",
            "proxies": all_proxies(db),
            "groups": all_provider_groups(db),
        },
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
    group_name: Annotated[str, Form()] = "",
) -> Response:
    provider = provider_or_404(db, provider_id)
    if blocked := reject_archived_provider(request, provider):
        return blocked
    errors = validate_provider_form(name, base_url, api_key, client_profile, require_key=False)
    clean_group_name, group_error = validate_group_name(group_name)
    if group_error:
        errors.append(group_error)
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
                "form": form_values(
                    name, base_url, notes, enabled, client_profile, default_proxy_id, clean_group_name
                ),
                "proxies": all_proxies(db),
                "groups": all_provider_groups(db),
            },
            400,
        )

    old_group_id = provider.group_id
    group = resolve_provider_group(db, clean_group_name)
    provider.name = name.strip()
    provider.base_url = normalize_base_url(base_url)
    provider.notes = notes.strip()
    provider.enabled = enabled == "on"
    provider.client_profile = client_profile
    provider.default_proxy_id = default_proxy.id if default_proxy else None
    provider.group_id = group.id if group else None
    if api_key.strip():
        fernet = require_session_fernet(request)
        provider.encrypted_api_key = encrypt_api_key_with_fernet(api_key.strip(), fernet)
        provider.key_hint = key_hint(api_key)

    try:
        delete_group_if_empty(db, old_group_id if old_group_id != provider.group_id else None)
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
                "form": form_values(
                    name, base_url, notes, enabled, client_profile, default_proxy_id, clean_group_name
                ),
                "proxies": all_proxies(db),
                "groups": all_provider_groups(db),
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
    old_group_id = provider.group_id
    provider_name = provider.name
    db.execute(
        update(ScheduledTaskProvider)
        .where(ScheduledTaskProvider.provider_id == provider.id)
        .values(provider_id=None)
    )
    db.delete(provider)
    delete_group_if_empty(db, old_group_id)
    db.commit()
    flash(request, f"“{provider_name}”已永久删除。" if was_archived else "中转站已删除。")
    return redirect("/archive" if was_archived else "/")


@app.post("/providers/{provider_id}/models/manual")
def provider_add_manual_model(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
    model_id: Annotated[str, Form()] = "",
) -> Response:
    provider = provider_or_404(db, provider_id)
    if blocked := reject_archived_provider(request, provider):
        return blocked
    clean_model_id = model_id.strip()
    if not clean_model_id:
        flash(request, "手动模型名称不能为空。", "error")
        return redirect(f"/providers/{provider.id}")
    if len(clean_model_id) > 260:
        flash(request, "手动模型名称不能超过 260 个字符。", "error")
        return redirect(f"/providers/{provider.id}")

    model = next((item for item in provider.models if item.model_id == clean_model_id), None)
    if model is not None:
        if model.is_manual:
            flash(request, f"手动模型“{clean_model_id}”已存在。")
            return redirect(f"/providers/{provider.id}")
        model.is_manual = True
        model.last_seen_at = utc_now()
        message = f"模型“{clean_model_id}”已转为手动模型。"
    else:
        db.add(
            ProviderModel(
                provider_id=provider.id,
                model_id=clean_model_id,
                is_manual=True,
                owned_by="",
                raw_json=compact_json({"id": clean_model_id, "source": "manual"}),
                last_seen_at=utc_now(),
            )
        )
        message = f"手动模型“{clean_model_id}”已添加。"
    db.commit()
    flash(request, message)
    return redirect(f"/providers/{provider.id}")


@app.post("/providers/{provider_id}/models/{provider_model_id}/delete")
def provider_delete_manual_model(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider_id: int,
    provider_model_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    provider = provider_or_404(db, provider_id)
    if blocked := reject_archived_provider(request, provider):
        return blocked
    model = db.scalar(
        select(ProviderModel).where(
            ProviderModel.id == provider_model_id,
            ProviderModel.provider_id == provider.id,
        )
    )
    if model is None:
        raise HTTPException(status_code=404, detail="模型不存在。")
    if not model.is_manual:
        flash(request, "接口刷新得到的模型不能手动删除。", "error")
        return redirect(f"/providers/{provider.id}")

    removed_model_id = model.model_id
    db.delete(model)
    select_next_test_model(db, provider, {removed_model_id})
    db.commit()
    flash(request, f"手动模型“{removed_model_id}”已删除。")
    return redirect(f"/providers/{provider.id}")


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
    refreshed_model_ids = {model.model_id for model in models}
    removed_model_ids: set[str] = set()
    for model in provider.models:
        if not model.is_manual and model.model_id not in refreshed_model_ids:
            removed_model_ids.add(model.model_id)
            db.delete(model)
    now = utc_now()
    for model_info in models:
        model = existing.get(model_info.model_id)
        if model is None:
            db.add(
                ProviderModel(
                    provider_id=provider.id,
                    model_id=model_info.model_id,
                    is_manual=False,
                    owned_by=model_info.owned_by,
                    raw_json=compact_json(model_info.raw_json),
                    last_seen_at=now,
                )
            )
        else:
            model.owned_by = model_info.owned_by
            model.raw_json = compact_json(model_info.raw_json)
            model.last_seen_at = now
    select_next_test_model(db, provider, removed_model_ids)
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
    return await run_provider_connectivity_test(
        db,
        provider,
        model_id,
        client_profile,
        network_route,
        require_session_fernet(request),
        trigger_source="manual",
        connectivity_runner=run_connectivity_test,
    )


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


def scheduled_task_or_404(db: Session, task_id: int) -> ScheduledTask:
    task = db.scalar(
        select(ScheduledTask)
        .where(ScheduledTask.id == task_id)
        .options(
            selectinload(ScheduledTask.group),
            selectinload(ScheduledTask.provider_links),
            selectinload(ScheduledTask.runs),
        )
    )
    if task is None:
        raise HTTPException(status_code=404, detail="定时任务不存在。")
    return task


def schedule_page_context(
    db: Session,
    *,
    form_values: dict[str, Any] | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    tasks = list(
        db.scalars(
            select(ScheduledTask)
            .options(
                selectinload(ScheduledTask.group),
                selectinload(ScheduledTask.provider_links),
                selectinload(ScheduledTask.runs),
            )
            .order_by(ScheduledTask.created_at, ScheduledTask.id)
        ).all()
    )
    recent_runs = list(db.scalars(select(ScheduledRun).order_by(ScheduledRun.started_at.desc()).limit(20)).all())
    providers = list(
        db.scalars(
            select(Provider)
            .where(Provider.archived_at.is_(None))
            .order_by(Provider.name)
        ).all()
    )
    return {
        "tasks": tasks,
        "recent_runs": recent_runs,
        "providers": providers,
        "groups": all_provider_groups(db),
        "vault_state": scheduler_vault_state(),
        "task_target_summary": task_target_summary,
        "task_schedule_summary": task_schedule_summary,
        "task_is_running": task_is_running,
        "form_values": form_values or {},
        "errors": errors or [],
        "app_timezone": settings.app_timezone,
    }


def parse_schedule_form(
    db: Session,
    *,
    name: str,
    target_type: str,
    group_id: str,
    provider_ids: list[int] | None,
    schedule_kind: str,
    interval_minutes: str,
    daily_time: str,
) -> tuple[dict[str, Any], list[str], list[Provider]]:
    errors: list[str] = []
    clean_provider_ids = list(dict.fromkeys(provider_ids or []))
    try:
        parsed_group_id = int(group_id) if group_id.strip() else None
    except ValueError:
        parsed_group_id = None
        errors.append("分组无效。")
    try:
        parsed_interval = int(interval_minutes) if interval_minutes.strip() else None
    except ValueError:
        parsed_interval = None
        errors.append("间隔必须是整数分钟。")
    values = {
        "name": name.strip(),
        "target_type": target_type,
        "group_id": parsed_group_id,
        "provider_ids": clean_provider_ids,
        "schedule_kind": schedule_kind,
        "interval_minutes": parsed_interval,
        "daily_time": daily_time.strip(),
    }
    errors.extend(validate_schedule_values(**values))
    if target_type == "group" and parsed_group_id is not None and db.get(ProviderGroup, parsed_group_id) is None:
        errors.append("所选分组不存在。")
    selected_providers = list(
        db.scalars(select(Provider).where(Provider.id.in_(clean_provider_ids)).order_by(Provider.name)).all()
    ) if clean_provider_ids else []
    if target_type == "providers" and len(selected_providers) != len(clean_provider_ids):
        errors.append("部分所选中转站不存在。")
    return values, errors, selected_providers


def apply_schedule_values(
    task: ScheduledTask,
    values: dict[str, Any],
    selected_providers: list[Provider],
    *,
    enabled: bool,
) -> None:
    task.name = values["name"]
    task.target_type = values["target_type"]
    task.group_id = values["group_id"] if values["target_type"] == "group" else None
    task.schedule_kind = values["schedule_kind"]
    task.interval_minutes = values["interval_minutes"] if values["schedule_kind"] == "interval" else None
    task.daily_time = values["daily_time"] if values["schedule_kind"] == "daily" else None
    task.timezone_name = settings.app_timezone
    task.enabled = enabled
    set_task_provider_links(task, selected_providers if values["target_type"] == "providers" else [])
    task.next_run_at = calculate_next_run(task) if enabled else None


@app.get("/schedules", response_class=HTMLResponse)
def schedules_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    return render(request, "schedules.html", schedule_page_context(db))


@app.post("/schedules/authorize")
def schedules_authorize(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    password: Annotated[str, Form()],
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    if not settings.session_secret_configured:
        flash(request, "必须在 .env 中显式设置稳定的 SESSION_SECRET 后才能启用后台任务。", "error")
        return redirect("/schedules")
    if not password or not authenticate(db, password) or not check_session_password_proof(request, db, password):
        flash(request, "密码确认失败，后台密钥未授权。", "error")
        return redirect("/schedules")
    authorize_scheduler_vault(db, password, settings.session_secret)
    for task in db.scalars(select(ScheduledTask).where(ScheduledTask.enabled.is_(True))).all():
        if task.next_run_at is None:
            task.next_run_at = calculate_next_run(task)
    db.commit()
    flash(request, "后台密钥已授权，定时任务可在应用重启后继续运行。")
    return redirect("/schedules")


@app.post("/schedules/lock")
def schedules_lock(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    clear_scheduler_vault(db)
    for task in db.scalars(select(ScheduledTask)).all():
        task.enabled = False
        task.next_run_at = None
    db.commit()
    flash(request, "后台密钥已锁定，所有定时任务已停用。")
    return redirect("/schedules")


@app.post("/schedules")
def schedule_create(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()] = "",
    target_type: Annotated[str, Form()] = "all",
    group_id: Annotated[str, Form()] = "",
    provider_ids: Annotated[list[int] | None, Form()] = None,
    schedule_kind: Annotated[str, Form()] = "interval",
    interval_minutes: Annotated[str, Form()] = "60",
    daily_time: Annotated[str, Form()] = "09:00",
    enabled: Annotated[str | None, Form()] = None,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    values, errors, selected_providers = parse_schedule_form(
        db,
        name=name,
        target_type=target_type,
        group_id=group_id,
        provider_ids=provider_ids,
        schedule_kind=schedule_kind,
        interval_minutes=interval_minutes,
        daily_time=daily_time,
    )
    should_enable = enabled == "on"
    if should_enable and scheduler_vault_state() != "ready":
        errors.append("请先授权后台密钥，再启用任务。")
    if errors:
        return render(
            request,
            "schedules.html",
            schedule_page_context(db, form_values={**values, "enabled": should_enable}, errors=errors),
            400,
        )
    task = ScheduledTask()
    apply_schedule_values(task, values, selected_providers, enabled=should_enable)
    db.add(task)
    db.commit()
    flash(request, f"定时任务“{task.name}”已创建。")
    return redirect("/schedules")


@app.get("/schedules/{task_id}/edit", response_class=HTMLResponse)
def schedule_edit_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    task_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    task = scheduled_task_or_404(db, task_id)
    return render(
        request,
        "schedule_form.html",
        {
            "task": task,
            "providers": list(db.scalars(select(Provider).where(Provider.archived_at.is_(None)).order_by(Provider.name)).all()),
            "groups": all_provider_groups(db),
            "errors": [],
            "app_timezone": settings.app_timezone,
        },
    )


@app.post("/schedules/{task_id}")
def schedule_update(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    task_id: int,
    name: Annotated[str, Form()] = "",
    target_type: Annotated[str, Form()] = "all",
    group_id: Annotated[str, Form()] = "",
    provider_ids: Annotated[list[int] | None, Form()] = None,
    schedule_kind: Annotated[str, Form()] = "interval",
    interval_minutes: Annotated[str, Form()] = "60",
    daily_time: Annotated[str, Form()] = "09:00",
    enabled: Annotated[str | None, Form()] = None,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    task = scheduled_task_or_404(db, task_id)
    values, errors, selected_providers = parse_schedule_form(
        db,
        name=name,
        target_type=target_type,
        group_id=group_id,
        provider_ids=provider_ids,
        schedule_kind=schedule_kind,
        interval_minutes=interval_minutes,
        daily_time=daily_time,
    )
    should_enable = enabled == "on"
    if should_enable and scheduler_vault_state() != "ready":
        errors.append("请先授权后台密钥，再启用任务。")
    if errors:
        return render(
            request,
            "schedule_form.html",
            {
                "task": task,
                "providers": list(db.scalars(select(Provider).where(Provider.archived_at.is_(None)).order_by(Provider.name)).all()),
                "groups": all_provider_groups(db),
                "errors": errors,
                "form_values": {**values, "enabled": should_enable},
                "app_timezone": settings.app_timezone,
            },
            400,
        )
    apply_schedule_values(task, values, selected_providers, enabled=should_enable)
    db.commit()
    flash(request, f"定时任务“{task.name}”已更新。")
    return redirect("/schedules")


@app.post("/schedules/{task_id}/toggle")
def schedule_toggle(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    task_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    task = scheduled_task_or_404(db, task_id)
    if not task.enabled and scheduler_vault_state() != "ready":
        flash(request, "后台密钥尚未授权或已锁定，不能启用任务。", "error")
        return redirect("/schedules")
    task.enabled = not task.enabled
    task.next_run_at = calculate_next_run(task) if task.enabled else None
    db.commit()
    flash(request, f"定时任务“{task.name}”已{'启用' if task.enabled else '停用'}。")
    return redirect("/schedules")


@app.post("/schedules/{task_id}/run")
async def schedule_run_now(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    task_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    task = scheduled_task_or_404(db, task_id)
    fernet = get_scheduler_fernet(db, settings.session_secret) or require_session_fernet(request)
    if enqueue_task_run(task.id, trigger="manual", fernet=fernet):
        flash(request, f"定时任务“{task.name}”已开始立即运行。")
    else:
        flash(request, f"定时任务“{task.name}”正在运行，请稍后再试。", "error")
    return redirect("/schedules")


@app.post("/schedules/{task_id}/delete")
def schedule_delete(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    task_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    task = scheduled_task_or_404(db, task_id)
    if task_is_running(task.id):
        flash(request, "任务正在运行，不能删除。", "error")
        return redirect("/schedules")
    name = task.name
    db.execute(update(ScheduledRun).where(ScheduledRun.task_id == task.id).values(task_id=None))
    db.delete(task)
    db.commit()
    flash(request, f"定时任务“{name}”已删除，历史测试记录已保留。")
    return redirect("/schedules")


@app.get("/statistics", response_class=HTMLResponse)
def statistics_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    range_key: Annotated[str, Query(alias="range")] = "7d",
    provider_id: str = "all",
    source: str = "all",
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    query_has_filter_params = any(name in request.query_params for name in ("range", "provider_id", "source"))
    stored_filters, has_stored_filters = read_statistics_filter_preferences(db)
    if not query_has_filter_params:
        range_key = stored_filters["range"]
        provider_id = stored_filters["provider_id"]
        source = stored_filters["source"]
    providers = list(db.scalars(select(Provider).order_by(Provider.name)).all())
    provider_ids = {provider.id for provider in providers}
    range_key, provider_id, source, selected_provider_id = normalize_statistics_filters(
        range_key=range_key,
        provider_id=provider_id,
        source=source,
        valid_provider_ids=provider_ids,
    )
    normalized_filters = {"range": range_key, "provider_id": provider_id, "source": source}
    stored_filters_json = get_setting(db, SETTING_STATISTICS_FILTER_PREFERENCES)
    normalized_filters_json = json.dumps(normalized_filters, ensure_ascii=False, sort_keys=True)
    if query_has_filter_params or (has_stored_filters and stored_filters_json != normalized_filters_json):
        write_statistics_filter_preferences(db, normalized_filters)
        db.commit()
    records = load_real_records(
        db,
        range_key=range_key,
        provider_id=selected_provider_id,
        source=source,
    )
    statistics = build_statistics(records, range_key=range_key)
    chart_data_json = json.dumps(statistics["chart_data"], ensure_ascii=False).replace("</", "<\\/")
    return render(
        request,
        "statistics.html",
        {
            **statistics,
            "chart_data_json": chart_data_json,
            "provider_options": providers,
            "provider_rows": statistics["providers"],
            "filters": {
                "range": range_key,
                "provider_id": provider_id,
                "source": source,
            },
            "timezone_name": settings.app_timezone,
        },
    )


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
        .options(
            selectinload(Provider.models),
            selectinload(Provider.tests),
            selectinload(Provider.default_proxy),
            selectinload(Provider.group),
        )
        .order_by(Provider.name)
    ).all()
    scheduled_tasks = db.scalars(
        select(ScheduledTask)
        .options(
            selectinload(ScheduledTask.group),
            selectinload(ScheduledTask.provider_links),
        )
        .order_by(ScheduledTask.name)
    ).all()
    payload = {
        "version": 6,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "contains_secrets": include_secret_values,
        "groups": [],
        "proxies": [],
        "providers": [],
        "schedules": [],
    }
    for group in all_provider_groups(db):
        payload["groups"].append({"name": group.name, "created_at": group.created_at.isoformat()})
    for proxy in all_proxies(db):
        username = decrypt_secret_with_fernet(proxy.encrypted_username, fernet) if include_secret_values else ""
        proxy_password = decrypt_secret_with_fernet(proxy.encrypted_password, fernet) if include_secret_values else ""
        payload["proxies"].append(serialize_proxy(proxy, include_secret_values, username, proxy_password))
    for provider in providers:
        api_key = decrypt_api_key_with_fernet(provider.encrypted_api_key, fernet) if include_secret_values else None
        exported_route = serialize_test_network_route(db, provider.test_network_route or "default")
        payload["providers"].append(serialize_provider(provider, include_secret_values, api_key, exported_route))
    for task in scheduled_tasks:
        payload["schedules"].append(
            {
                "name": task.name,
                "enabled": task.enabled,
                "target_type": task.target_type,
                "group": task.group.name if task.group else None,
                "providers": [
                    {"name": link.provider_name_snapshot, "base_url": link.provider_base_url_snapshot}
                    for link in task.provider_links
                ],
                "schedule_kind": task.schedule_kind,
                "interval_minutes": task.interval_minutes,
                "daily_time": task.daily_time,
                "timezone": task.timezone_name,
            }
        )

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
    groups = payload.get("groups", []) if isinstance(payload, dict) else None
    schedules = payload.get("schedules", []) if isinstance(payload, dict) else None
    if not isinstance(providers, list):
        flash(request, "导入失败：JSON 必须包含 providers 数组。", "error")
        return redirect("/import-export")
    if not isinstance(proxies, list):
        flash(request, "导入失败：proxies 必须是数组。", "error")
        return redirect("/import-export")
    if not isinstance(groups, list):
        flash(request, "导入失败：groups 必须是数组。", "error")
        return redirect("/import-export")
    if not isinstance(schedules, list):
        flash(request, "导入失败：schedules 必须是数组。", "error")
        return redirect("/import-export")

    fernet = require_session_fernet(request)
    created = 0
    updated = 0
    proxy_created = 0
    proxy_updated = 0
    group_created = 0
    schedule_created = 0
    schedule_updated = 0
    errors: list[str] = []
    for index, item in enumerate(groups, start=1):
        if not isinstance(item, dict):
            errors.append(f"分组第 {index} 条：分组必须是对象。")
            continue
        group_name, group_error = validate_group_name(str(item.get("name") or ""))
        if group_error or not group_name:
            errors.append(f"分组第 {index} 条：{group_error or '分组名称不能为空。'}")
            continue
        normalized_name = group_name.casefold()
        group = db.scalar(select(ProviderGroup).where(ProviderGroup.normalized_name == normalized_name))
        was_created = group is None
        if group is None:
            group = ProviderGroup(name=group_name, normalized_name=normalized_name)
            db.add(group)
            db.flush()
            group_created += 1
        created_at_value = item.get("created_at")
        if created_at_value not in (None, ""):
            try:
                if not isinstance(created_at_value, str):
                    raise ValueError("created_at 必须是 ISO 8601 时间字符串")
                created_at = datetime.fromisoformat(created_at_value.replace("Z", "+00:00"))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                group.created_at = created_at.astimezone(timezone.utc)
            except (TypeError, ValueError) as exc:
                errors.append(f"分组第 {index} 条：created_at 无效（{exc}）。")
                if was_created:
                    db.delete(group)
                    group_created -= 1
                continue
        db.flush()

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
        imported_group_name = item.get("group")
        provider_group: ProviderGroup | None = None
        if imported_group_name not in (None, ""):
            if not isinstance(imported_group_name, str):
                errors.append(f"第 {index} 条：group 必须是分组名称或 null。")
                continue
            clean_group_name, group_error = validate_group_name(imported_group_name)
            if group_error or not clean_group_name:
                errors.append(f"第 {index} 条：{group_error or 'group 不能为空白。'}")
                continue
            provider_group = db.scalar(
                select(ProviderGroup).where(ProviderGroup.normalized_name == clean_group_name.casefold())
            )
            if provider_group is None:
                errors.append(f"第 {index} 条：找不到分组“{clean_group_name}”。")
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
                group_id=provider_group.id if provider_group else None,
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
            provider.group_id = provider_group.id if provider_group else None
            provider.archived_at = archived_at
            if api_key:
                provider.encrypted_api_key = encrypt_api_key_with_fernet(api_key, fernet)
                provider.key_hint = key_hint(api_key)
            updated += 1

        model_count = upsert_models(db, provider, item.get("models") if isinstance(item.get("models"), list) else [])
        if model_count:
            provider.updated_at = utc_now()

    db.flush()
    for index, item in enumerate(schedules, start=1):
        if not isinstance(item, dict):
            errors.append(f"定时任务第 {index} 条：任务必须是对象。")
            continue
        task_name = str(item.get("name") or "").strip()
        target_type = str(item.get("target_type") or "all").strip()
        schedule_kind = str(item.get("schedule_kind") or "interval").strip()
        interval_value = item.get("interval_minutes")
        try:
            interval_minutes = int(interval_value) if interval_value not in (None, "") else None
        except (TypeError, ValueError):
            interval_minutes = None
        daily_time = str(item.get("daily_time") or "").strip() or None
        task_group: ProviderGroup | None = None
        if target_type == "group":
            group_name = str(item.get("group") or "").strip()
            task_group = db.scalar(
                select(ProviderGroup).where(ProviderGroup.normalized_name == group_name.casefold())
            )
            if task_group is None:
                errors.append(f"定时任务第 {index} 条：找不到分组“{group_name}”。")
                continue
        selected_providers: list[Provider] = []
        if target_type == "providers":
            raw_targets = item.get("providers", [])
            if not isinstance(raw_targets, list):
                errors.append(f"定时任务第 {index} 条：providers 必须是数组。")
                continue
            for target in raw_targets:
                if not isinstance(target, dict):
                    continue
                target_name = str(target.get("name") or "").strip()
                target_url = normalize_base_url(str(target.get("base_url") or ""))
                provider = db.scalar(
                    select(Provider).where(Provider.name == target_name, Provider.base_url == target_url)
                )
                if provider is not None:
                    selected_providers.append(provider)
            if not selected_providers:
                errors.append(f"定时任务第 {index} 条：没有找到可映射的指定中转站。")
                continue
        task_errors = validate_schedule_values(
            name=task_name,
            target_type=target_type,
            group_id=task_group.id if task_group else None,
            provider_ids=[provider.id for provider in selected_providers],
            schedule_kind=schedule_kind,
            interval_minutes=interval_minutes,
            daily_time=daily_time,
        )
        if task_errors:
            errors.append(f"定时任务第 {index} 条：{' '.join(task_errors)}")
            continue
        task = db.scalar(select(ScheduledTask).where(ScheduledTask.name == task_name))
        if task is None:
            task = ScheduledTask()
            db.add(task)
            schedule_created += 1
        else:
            schedule_updated += 1
        values = {
            "name": task_name,
            "target_type": target_type,
            "group_id": task_group.id if task_group else None,
            "schedule_kind": schedule_kind,
            "interval_minutes": interval_minutes,
            "daily_time": daily_time,
        }
        apply_schedule_values(task, values, selected_providers, enabled=False)

    for group in all_provider_groups(db):
        delete_group_if_empty(db, group.id)
    db.commit()
    message = (
        f"导入完成：分组新增 {group_created} 个；代理新增 {proxy_created} 个、更新 {proxy_updated} 个；"
        f"中转站新增 {created} 个、更新 {updated} 个；定时任务新增 {schedule_created} 个、更新 {schedule_updated} 个（均已停用）"
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
    group_name: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "base_url": base_url,
        "notes": notes,
        "enabled": enabled == "on",
        "client_profile": client_profile,
        "default_proxy_id": default_proxy_id,
        "group_name": group_name,
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
