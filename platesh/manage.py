"""Управление с сервера: доступы, оплаты, возвраты Stars.

Команды обращаются к запущенному Platesh через его HTTP API на этом же сервере (главный ключ
server.api_key из config.yaml), поэтому работают и для ботов со своим кодом, где нет кнопок админа.
Библиотеки ботов не загружаются — команды быстрые даже на слабом сервере.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .config import BotConfig, Config
from .texts import format_date, format_price

SERVICE_HINT = "Проверь, что служба работает: systemctl status platesh"


class ManageError(Exception):
    pass


def api(cfg: Config, method: str, path: str, *, body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None) -> Any:
    if not cfg.server.enabled:
        raise ManageError("HTTP API выключен (server.enabled: false), а команды управления работают через него")
    if not cfg.server.api_key:
        raise ManageError("в config.yaml не задан server.api_key — перенастрой Platesh: ./run.sh setup --no-start")
    host = cfg.server.host if cfg.server.host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    url = f"http://{f'[{host}]' if ':' in host else host}:{cfg.server.port}{path}"
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {cfg.server.api_key}", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # свой сервер — без прокси
    try:
        with opener.open(request, timeout=60) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.load(exc)
        except ValueError:
            raise ManageError(f"Platesh ответил HTTP {exc.code}. {SERVICE_HINT}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ManageError(f"Platesh не отвечает ({url}): {exc}. {SERVICE_HINT}") from None
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise ManageError(payload.get("error", "неизвестная ошибка") if isinstance(payload, dict) else "странный ответ")
    return payload["result"]


def pick_bot(cfg: Config, name: str | None) -> BotConfig:
    if name:
        bot = next((b for b in cfg.bots if b.name == name), None)
        if bot is None:
            raise ManageError(f"нет бота «{name}». Есть: {', '.join(b.name for b in cfg.bots)}")
        return bot
    if len(cfg.bots) == 1:
        return cfg.bots[0]
    raise ManageError(f"ботов несколько — укажи, какой: --bot {cfg.bots[0].name}  (есть: {', '.join(b.name for b in cfg.bots)})")


def pick_product(bot: BotConfig, product_id: str | None) -> str:
    if product_id:
        if product_id not in bot.products:
            raise ManageError(f"у бота «{bot.name}» нет товара «{product_id}». Есть: {', '.join(bot.products)}")
        return product_id
    if len(bot.products) == 1:
        return next(iter(bot.products))
    raise ManageError(f"товаров несколько — укажи, какой: --product {next(iter(bot.products))}  (есть: {', '.join(bot.products)})")


def user_id_arg(params: list[str], index: int = 0) -> int:
    if len(params) <= index or not params[index].lstrip("-").isdigit():
        raise ManageError("укажи Telegram ID пользователя числом, например: grant 123456789")
    return int(params[index])


def _until(access: dict[str, Any]) -> str:
    if access["expires_at"] is None:
        return "навсегда"
    word = "до" if access["active"] else "закончился"
    return f"{word} {format_date(access['expires_at'])}"


def cmd_access(cfg: Config, params: list[str], bot_name: str | None, product_id: str | None) -> str:
    user_id = user_id_arg(params)
    bots = [pick_bot(cfg, bot_name)] if bot_name else cfg.bots
    lines = []
    for bot in bots:
        result = api(cfg, "GET", "/api/v1/access", query={"bot": bot.name, "user_id": user_id})
        if not result["accesses"]:
            lines.append(f"Бот «{bot.name}»: у {user_id} доступа нет")
            continue
        for a in result["accesses"]:
            title = bot.products[a["product_id"]].title if a["product_id"] in bot.products else a["product_id"]
            mark = "✅" if a["active"] else "❌"
            lines.append(f"Бот «{bot.name}»: {mark} {title} ({a['product_id']}) — {_until(a)}")
    return "\n".join(lines)


def cmd_grant(cfg: Config, params: list[str], bot_name: str | None, product_id: str | None) -> str:
    user_id = user_id_arg(params)
    days = None
    if len(params) > 1:
        if not params[1].isdigit() or int(params[1]) < 1:
            raise ManageError("срок — число дней больше нуля, например: grant 123456789 30")
        days = int(params[1])
    bot = pick_bot(cfg, bot_name)
    product = pick_product(bot, product_id)
    result = api(cfg, "POST", "/api/v1/access/grant",
                 body={"bot": bot.name, "user_id": user_id, "product_id": product, "days": days})
    told = "пользователю отправлено сообщение" if result.get("delivered") else "сообщение пользователю не дошло (он не писал боту?)"
    return f"✅ {user_id}: доступ к «{bot.products[product].title}» {_until(result)}, {told}"


def cmd_revoke(cfg: Config, params: list[str], bot_name: str | None, product_id: str | None) -> str:
    user_id = user_id_arg(params)
    bot = pick_bot(cfg, bot_name)
    product = pick_product(bot, product_id)
    result = api(cfg, "POST", "/api/v1/access/revoke", body={"bot": bot.name, "user_id": user_id, "product_id": product})
    if result.get("revoked"):
        return f"✅ {user_id}: доступ к «{bot.products[product].title}» закрыт"
    return f"У {user_id} не было доступа к «{bot.products[product].title}»"


def cmd_payments(cfg: Config, params: list[str], bot_name: str | None, product_id: str | None) -> str:
    user_id = user_id_arg(params) if params else None
    query = {"status": "paid", "limit": 30, "user_id": user_id, "bot": pick_bot(cfg, bot_name).name if bot_name else None}
    items = api(cfg, "GET", "/api/v1/invoices", query=query)
    if not items:
        return "Оплат пока нет" + (f" у {user_id}" if user_id else "")
    lines = [f"Последние оплаты ({len(items)}):"]
    for inv in items:
        when = format_date(inv["paid_at"]) if inv.get("paid_at") else "?"
        paid = f" ({inv['paid_amount']} {inv['paid_asset']})" if inv.get("paid_asset") else ""
        lines.append(f"{when}  {inv['user_id']}  {format_price(inv['amount'], inv['currency'])}{paid}  "
                     f"{inv['title']}  · бот {inv['bot']} · счёт {inv['id']}")
    lines.append("Вернуть Stars:  ./run.sh refund НОМЕР_СЧЁТА")
    return "\n".join(lines)


def cmd_refund(cfg: Config, params: list[str], bot_name: str | None, product_id: str | None) -> str:
    if not params:
        raise ManageError("укажи номер счёта (последняя колонка в ./run.sh payments): refund НОМЕР_СЧЁТА")
    inv = api(cfg, "POST", f"/api/v1/invoices/{urllib.parse.quote(params[0], safe='')}/refund")
    return f"✅ Возврат {format_price(inv['amount'], inv['currency'])} пользователю {inv['user_id']} сделан, доступ закрыт"


COMMANDS: dict[str, Callable[[Config, list[str], str | None, str | None], str]] = {
    "access": cmd_access,
    "grant": cmd_grant,
    "revoke": cmd_revoke,
    "payments": cmd_payments,
    "refund": cmd_refund,
}


def run_command(cfg: Config, command: str, params: list[str], *, bot: str | None = None, product: str | None = None) -> str:
    return COMMANDS[command](cfg, params, bot, product)
