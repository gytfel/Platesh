"""HTTP API Platesh и приём вебхуков CryptoBot."""

from __future__ import annotations

import hmac
import logging
from typing import Any, Awaitable, Callable

from aiohttp import web
from aiogram.exceptions import TelegramAPIError

from .bots.registry import BotRegistry, BotRuntime
from .config import PAYMENT_METHODS, Config
from .payments import PaymentError, PaymentService
from .texts import format_amount

log = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

def ok(result: Any) -> web.Response:
    return web.json_response({"ok": True, "result": result})


def fail(status: int, error: str) -> web.Response:
    return web.json_response({"ok": False, "error": error}, status=status)


class ApiError(Exception):
    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


def _int(value: Any, name: str, *, required: bool = False) -> int | None:
    if value is None or value == "":
        if required:
            raise ApiError(400, f"{name} обязателен")
        return None
    if isinstance(value, bool):
        raise ApiError(400, f"{name} должен быть целым числом")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{name} должен быть целым числом") from None


def _str(value: Any, name: str, *, max_len: int = 1024) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(400, f"{name} должен быть строкой")
    if len(value) > max_len:
        raise ApiError(400, f"{name}: максимум {max_len} символов")
    return value


async def _json(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        raise ApiError(400, "тело запроса должно быть JSON-объектом") from None
    if not isinstance(data, dict):
        raise ApiError(400, "тело запроса должно быть JSON-объектом")
    return data


def create_app(cfg: Config, registry: BotRegistry, payments: PaymentService) -> web.Application:
    api_key = cfg.server.api_key

    @web.middleware
    async def errors_and_auth(request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.path.startswith("/api/"):
            if not api_key:
                return fail(403, "API выключен: задай server.api_key в config.yaml")
            auth = request.headers.get("Authorization", "")
            given = auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("X-API-Key", "")
            if not hmac.compare_digest(given.encode(), api_key.encode()):
                return fail(401, "неверный API-ключ")
        try:
            return await handler(request)
        except ApiError as exc:
            return fail(exc.status, exc.error)
        except PaymentError as exc:
            return fail(400, str(exc))

    def get_bot(name: Any) -> BotRuntime:
        rt = registry.get(name) if isinstance(name, str) else None
        if rt is None:
            raise ApiError(404, f"бот «{name}» не найден (поле bot — это name из config.yaml)")
        return rt

    async def get_invoice_or_404(invoice_id: str):
        inv = await payments.db.get_invoice(invoice_id)
        if inv is None:
            raise ApiError(404, "счёт не найден")
        return inv

    # ------------------------------------------------------------------ routes

    async def health(request: web.Request) -> web.Response:
        return ok({"bots": len(registry.all())})

    async def list_bots(request: web.Request) -> web.Response:
        result = []
        for rt in registry.all():
            result.append({
                "name": rt.name,
                "id": rt.id,
                "username": rt.username,
                "mode": rt.cfg.mode,
                "payments": [m for m in PAYMENT_METHODS if rt.cfg.method_enabled(m)],
                "crypto_currency": rt.cfg.crypto.currency,
                "products": [
                    {
                        "id": p.id,
                        "title": p.title,
                        "description": p.description,
                        "price_stars": p.price_stars,
                        "price_crypto": format_amount(p.price_crypto) if p.price_crypto is not None else None,
                    }
                    for p in rt.cfg.products.values()
                ],
            })
        return ok(result)

    async def create_invoice(request: web.Request) -> web.Response:
        data = await _json(request)
        rt = get_bot(data.get("bot"))
        method = data.get("method")
        if method not in PAYMENT_METHODS:
            raise ApiError(400, "method: stars или crypto")
        user_id = _int(data.get("user_id"), "user_id")
        send = bool(data.get("send", False))
        if send and user_id is None:
            raise ApiError(400, "для send=true нужен user_id")
        product = None
        product_id = _str(data.get("product_id"), "product_id", max_len=32)
        if product_id:
            product = rt.cfg.products.get(product_id)
            if product is None:
                raise ApiError(404, f"товар «{product_id}» не найден у бота «{rt.name}»")
        amount = data.get("amount")
        if amount is not None and (isinstance(amount, bool) or not isinstance(amount, (int, float, str))):
            raise ApiError(400, "amount должен быть числом или строкой")
        inv = await payments.create_invoice(
            rt,
            method=method,
            user_id=user_id,
            product=product,
            amount=amount,
            currency=_str(data.get("currency"), "currency", max_len=10),
            title=_str(data.get("title"), "title", max_len=255),
            description=_str(data.get("description"), "description", max_len=1024),
            payload=_str(data.get("payload"), "payload", max_len=4096),
        )
        if send:
            try:
                await payments.send_invoice(rt, inv, user_id)
            except TelegramAPIError as exc:
                raise ApiError(400, f"счёт создан ({inv.id}), но отправить его не удалось: {exc.message}") from exc
        return ok(inv.to_dict())

    async def get_invoice(request: web.Request) -> web.Response:
        inv = await get_invoice_or_404(request.match_info["invoice_id"])
        if request.query.get("refresh") in ("1", "true"):
            inv = await payments.refresh(inv)
        return ok(inv.to_dict())

    async def list_invoices(request: web.Request) -> web.Response:
        q = request.query
        limit = _int(q.get("limit"), "limit") or 50
        items = await payments.db.list_invoices(
            bot=q.get("bot") or None,
            user_id=_int(q.get("user_id"), "user_id"),
            status=q.get("status") or None,
            limit=max(1, min(limit, 500)),
        )
        return ok([inv.to_dict() for inv in items])

    async def refund(request: web.Request) -> web.Response:
        inv = await get_invoice_or_404(request.match_info["invoice_id"])
        return ok((await payments.refund(inv)).to_dict())

    # Для ботов mode: external — их код пересылает сюда события оплаты Stars (см. client/platesh_client.py).
    async def stars_pre_checkout(request: web.Request) -> web.Response:
        data = await _json(request)
        rt = get_bot(data.get("bot"))
        error = await payments.validate_pre_checkout(
            rt,
            _str(data.get("payload"), "payload", max_len=128) or "",
            _str(data.get("currency"), "currency", max_len=10) or "",
            _int(data.get("total_amount"), "total_amount", required=True),
        )
        return ok({"accept": error is None, "error": error})

    async def stars_paid(request: web.Request) -> web.Response:
        data = await _json(request)
        rt = get_bot(data.get("bot"))
        charge_id = _str(data.get("charge_id"), "charge_id", max_len=256)
        if not charge_id:
            raise ApiError(400, "charge_id обязателен")
        user_id = _int(data.get("user_id"), "user_id", required=True)
        await payments.db.upsert_user(
            rt.name, user_id, _str(data.get("username"), "username", max_len=64), _str(data.get("first_name"), "first_name", max_len=256)
        )
        inv = await payments.process_stars_payment(
            rt,
            payload=_str(data.get("payload"), "payload", max_len=128) or "",
            charge_id=charge_id,
            total_amount=_int(data.get("total_amount"), "total_amount", required=True),
            currency=_str(data.get("currency"), "currency", max_len=10) or "",
            user_id=user_id,
        )
        return ok(inv.to_dict() if inv else None)

    async def cryptobot_webhook(request: web.Request) -> web.Response:
        body = await request.read()
        if not await payments.handle_crypto_webhook(body, request.headers.get("crypto-pay-api-signature")):
            log.warning("Вебхук CryptoBot с неверной подписью от %s", request.remote)
            return fail(401, "bad signature")
        return ok(True)

    app = web.Application(middlewares=[errors_and_auth], client_max_size=256 * 1024)
    app.add_routes([
        web.get("/health", health),
        web.post("/webhooks/cryptobot", cryptobot_webhook),
        web.get("/api/v1/bots", list_bots),
        web.post("/api/v1/invoices", create_invoice),
        web.get("/api/v1/invoices", list_invoices),
        web.get("/api/v1/invoices/{invoice_id}", get_invoice),
        web.post("/api/v1/invoices/{invoice_id}/refund", refund),
        web.post("/api/v1/stars/pre-checkout", stars_pre_checkout),
        web.post("/api/v1/stars/paid", stars_paid),
    ])
    return app
