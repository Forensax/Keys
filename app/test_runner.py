from __future__ import annotations

from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from .models import ConnectivityTest, NetworkProxy, Provider, utc_now
from .openai_compat import VALID_CLIENT_PROFILES, run_connectivity_test
from .proxy_support import build_proxy_url, sanitize_proxy_error
from .security import decrypt_api_key_with_fernet


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


def resolve_network_route_with_fernet(
    db: Session,
    provider: Provider,
    route: str,
    fernet: Fernet,
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
        raise ValueError(f"网络代理“{proxy.name}”已禁用，请启用代理或选择直连。")
    return build_proxy_url(proxy, fernet), proxy.name


async def run_provider_connectivity_test(
    db: Session,
    provider: Provider,
    model_id: str | None,
    client_profile: str,
    network_route: str,
    fernet: Fernet,
    *,
    trigger_source: str = "manual",
    scheduled_run_id: int | None = None,
    connectivity_runner=run_connectivity_test,
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
            proxy_url, route_label = resolve_network_route_with_fernet(
                db, provider, network_route, fernet
            )
        except Exception as exc:
            preflight_error = str(exc)

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
            trigger_source=trigger_source,
            scheduled_run_id=scheduled_run_id,
            tested_at=utc_now(),
        )
    else:
        try:
            api_key = decrypt_api_key_with_fernet(provider.encrypted_api_key, fernet)
            result = await connectivity_runner(
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
                trigger_source=trigger_source,
                scheduled_run_id=scheduled_run_id,
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
                trigger_source=trigger_source,
                scheduled_run_id=scheduled_run_id,
                tested_at=utc_now(),
            )

    db.add(test)
    db.commit()
    db.refresh(test)
    return test
