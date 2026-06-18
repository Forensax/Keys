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
from .models import ConnectivityTest, Provider, ProviderModel, utc_now
from .openai_compat import compact_json, fetch_models, normalize_base_url, run_chat_completion_test
from .security import (
    authenticate,
    check_session_password_proof,
    decrypt_api_key_with_fernet,
    encrypt_api_key_with_fernet,
    is_authenticated,
    is_initialized,
    key_hint,
    login_session,
    logout_session,
    require_session_fernet,
    setup_application,
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


def serialize_provider(provider: Provider, include_secret: bool = False, api_key: str | None = None) -> dict[str, Any]:
    latest_test = provider.tests[0] if provider.tests else None
    payload: dict[str, Any] = {
        "name": provider.name,
        "base_url": provider.base_url,
        "notes": provider.notes,
        "enabled": provider.enabled,
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
            "status": latest_test.status,
            "latency_ms": latest_test.latency_ms,
            "error_message": latest_test.error_message,
            "tested_at": latest_test.tested_at.isoformat(),
        }
    if include_secret:
        payload["api_key"] = api_key or ""
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


@app.get("/providers/new", response_class=HTMLResponse)
def provider_new(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(current_user_required)] = None,
) -> Response:
    return render(request, "provider_form.html", {"provider": None, "mode": "new"})


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
) -> Response:
    errors = validate_provider_form(name, base_url, api_key, require_key=True)
    if errors:
        return render(
            request,
            "provider_form.html",
            {"provider": None, "mode": "new", "errors": errors, "form": form_values(name, base_url, notes, enabled)},
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
                "form": form_values(name, base_url, notes, enabled),
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
    return render(request, "provider_detail.html", {"provider": provider})


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
    return render(request, "provider_form.html", {"provider": provider, "mode": "edit"})


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
) -> Response:
    provider = provider_or_404(db, provider_id)
    if blocked := reject_archived_provider(request, provider):
        return blocked
    errors = validate_provider_form(name, base_url, api_key, require_key=False)
    if errors:
        return render(
            request,
            "provider_form.html",
            {
                "provider": provider,
                "mode": "edit",
                "errors": errors,
                "form": form_values(name, base_url, notes, enabled),
            },
            400,
        )

    provider.name = name.strip()
    provider.base_url = normalize_base_url(base_url)
    provider.notes = notes.strip()
    provider.enabled = enabled == "on"
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
                "form": form_values(name, base_url, notes, enabled),
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
    try:
        api_key = decrypt_api_key_with_fernet(provider.encrypted_api_key, fernet)
        models = await fetch_models(provider.base_url, api_key)
    except httpx.HTTPStatusError as exc:
        flash(request, f"刷新模型失败：HTTP {exc.response.status_code} {exc.response.text[:300]}", "error")
        return redirect(f"/providers/{provider.id}")
    except Exception as exc:
        flash(request, f"刷新模型失败：{exc}", "error")
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
    flash(request, f"已获取 {len(models)} 个模型。")
    return redirect(f"/providers/{provider.id}")


@app.post("/providers/{provider_id}/test")
async def provider_test(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    provider_id: int,
    _: Annotated[None, Depends(current_user_required)] = None,
    model_id: Annotated[str, Form()] = "",
) -> Response:
    provider = provider_or_404(db, provider_id)
    if blocked := reject_archived_provider(request, provider):
        return blocked
    model_id = model_id.strip()
    if not model_id:
        flash(request, "请选择要测试的模型。", "error")
        return redirect(f"/providers/{provider.id}")

    fernet = require_session_fernet(request)
    try:
        api_key = decrypt_api_key_with_fernet(provider.encrypted_api_key, fernet)
        result = await run_chat_completion_test(provider.base_url, api_key, model_id)
    except Exception as exc:
        result = None
        test = ConnectivityTest(
            provider_id=provider.id,
            model_id=model_id,
            status="failed",
            latency_ms=None,
            error_message=str(exc),
            raw_response_excerpt="",
            tested_at=utc_now(),
        )
        db.add(test)
        db.commit()
        flash(request, f"连通性测试失败：{exc}", "error")
        return redirect(f"/providers/{provider.id}")

    test = ConnectivityTest(
        provider_id=provider.id,
        model_id=model_id,
        status=result.status,
        latency_ms=result.latency_ms,
        error_message=result.error_message,
        raw_response_excerpt=result.raw_response_excerpt,
        tested_at=utc_now(),
    )
    db.add(test)
    db.commit()
    if result.status == "success":
        flash(request, f"连通性测试成功，耗时 {result.latency_ms} ms。")
    else:
        flash(request, f"连通性测试失败：{result.error_message}", "error")
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
            flash(request, "密码确认失败，未导出明文 API Key。", "error")
            return redirect("/import-export")

    providers = db.scalars(
        select(Provider).options(selectinload(Provider.models), selectinload(Provider.tests)).order_by(Provider.name)
    ).all()
    payload = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "contains_secrets": include_secret_values,
        "providers": [],
    }
    for provider in providers:
        api_key = decrypt_api_key_with_fernet(provider.encrypted_api_key, fernet) if include_secret_values else None
        payload["providers"].append(serialize_provider(provider, include_secret_values, api_key))

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
    if not isinstance(providers, list):
        flash(request, "导入失败：JSON 必须包含 providers 数组。", "error")
        return redirect("/import-export")

    fernet = require_session_fernet(request)
    created = 0
    updated = 0
    errors: list[str] = []
    for index, item in enumerate(providers, start=1):
        if not isinstance(item, dict):
            errors.append(f"第 {index} 条：中转站必须是对象。")
            continue
        name = str(item.get("name") or "").strip()
        base_url = normalize_base_url(str(item.get("base_url") or ""))
        notes = str(item.get("notes") or "").strip()
        enabled = bool(item.get("enabled", True))
        api_key = str(item.get("api_key") or "").strip()
        try:
            archived_at = parse_archived_at(item.get("archived_at"))
        except (TypeError, ValueError) as exc:
            errors.append(f"第 {index} 条：archived_at 无效（{exc}）。")
            continue
        if not name or not base_url:
            errors.append(f"第 {index} 条：name 和 base_url 必填。")
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
                archived_at=archived_at,
            )
            db.add(provider)
            db.flush()
            created += 1
        else:
            provider.notes = notes
            provider.enabled = enabled
            provider.archived_at = archived_at
            if api_key:
                provider.encrypted_api_key = encrypt_api_key_with_fernet(api_key, fernet)
                provider.key_hint = key_hint(api_key)
            updated += 1

        model_count = upsert_models(db, provider, item.get("models") if isinstance(item.get("models"), list) else [])
        if model_count:
            provider.updated_at = utc_now()

    db.commit()
    message = f"导入完成：新增 {created} 个，更新 {updated} 个"
    if errors:
        message += f"，{len(errors)} 个错误。" + " ".join(errors[:5])
        flash(request, message, "error")
    else:
        flash(request, message + "。")
    return redirect("/import-export")


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


def validate_provider_form(name: str, base_url: str, api_key: str, require_key: bool) -> list[str]:
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
    return errors


def form_values(name: str, base_url: str, notes: str, enabled: str | None) -> dict[str, Any]:
    return {"name": name, "base_url": base_url, "notes": notes, "enabled": enabled == "on"}
