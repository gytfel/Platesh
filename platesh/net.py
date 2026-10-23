"""Прокси для связи с Telegram и CryptoBot (там, где api.telegram.org заблокирован)."""

from __future__ import annotations

import urllib.parse
import urllib.request

AUTO = "auto"
DIRECT = ("off", "none", "direct", "no", "false")


def system_proxy() -> str | None:
    """Прокси из настроек системы: в Windows — то, что выставил VPN-клиент («системный прокси»),
    в Linux/macOS — переменные HTTPS_PROXY/HTTP_PROXY/ALL_PROXY."""
    proxies = urllib.request.getproxies()
    for key in ("https", "http", "all", "socks"):
        if proxies.get(key):
            return normalize_proxy(proxies[key])
    return None


def normalize_proxy(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = f"http://{value}"  # «127.0.0.1:10809» — обычный HTTP-прокси
    scheme, rest = value.split("://", 1)
    scheme = scheme.lower()
    if scheme == "https":
        scheme = "http"  # Windows подписывает HTTP-прокси как https://, но говорить с ним надо по HTTP
    elif scheme == "socks":
        scheme = "socks5"
    elif scheme == "socks5h":
        scheme = "socks5"
    return f"{scheme}://{rest}"


def resolve_proxy(setting: str | None) -> str | None:
    """auto (по умолчанию) — системный прокси, если он есть; off — напрямую; иначе — указанный адрес."""
    if setting is None or setting.strip().lower() in ("", AUTO):
        return system_proxy()
    if setting.strip().lower() in DIRECT:
        return None
    return normalize_proxy(setting)


def mask_proxy(proxy: str) -> str:
    """Адрес прокси для логов — без логина и пароля."""
    parts = urllib.parse.urlsplit(proxy)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


def proxy_errors() -> tuple[type[BaseException], ...]:
    """Ошибки библиотеки прокси: они не наследуют ошибки aiohttp, поэтому ловим их отдельно."""
    try:
        from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError
    except ImportError:
        return ()
    return ProxyError, ProxyConnectionError, ProxyTimeoutError


def proxy_connector(proxy: str):
    """aiohttp-коннектор через прокси (HTTP, SOCKS4/5). Создавать внутри работающего event loop."""
    from aiohttp_socks import ProxyConnector

    return ProxyConnector.from_url(proxy)
