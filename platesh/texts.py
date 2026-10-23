"""Тексты интерфейса по умолчанию. Любой ключ можно переопределить в config.yaml (bots[].texts)."""

from __future__ import annotations

from decimal import Decimal

DEFAULT_TEXTS: dict[str, str] = {
    "welcome": "👋 Привет! Выбери, что хочешь купить:",
    "no_products": "Пока здесь нечего купить.",
    "admin_hint": "\n\n<i>Ты администратор — команды: /admin</i>",
    "product": "<b>{title}</b>\n\n{description}\n\nЦена: {price}",
    "pay_stars": "⭐ Оплатить {amount} Stars",
    "pay_crypto": "💎 Оплатить криптой — {amount} {currency}",
    "back": "« Назад",
    "crypto_invoice": (
        "💎 Счёт на <b>{amount} {currency}</b> создан.\n\n"
        "Нажми «Оплатить» и подтверди платёж в @CryptoBot. "
        "Как только оплата пройдёт, я пришлю покупку автоматически.\n\n"
        "Счёт действителен {minutes} мин."
    ),
    "crypto_pay_button": "💎 Оплатить",
    "check_button": "🔄 Проверить оплату",
    "not_paid_yet": "Оплата ещё не поступила. Если уже оплатил — подожди несколько секунд и проверь снова.",
    "invoice_expired": "Срок счёта истёк. Создай новый.",
    "already_paid": "Этот счёт уже оплачен ✅",
    "paid": "✅ Оплата получена! Спасибо за покупку.",
    "invite": "🔗 Ссылка для входа (одноразовая, действует {hours} ч.):\n{link}",
    "purchases_empty": "У тебя пока нет покупок.",
    "purchases_title": "🧾 Твои покупки:",
    "support": "По вопросам оплаты и возвратов напиши администратору бота.",
    "payment_error": "Не удалось создать счёт. Попробуй позже.",
    "method_unavailable": "Этот способ оплаты сейчас недоступен.",
    "precheckout_error": "Счёт недействителен или уже оплачен. Создай новый.",
    "refunded": "↩️ Оплата по счёту «{title}» возвращена: {amount}.",
}

CURRENCY_SIGNS = {"XTR": "⭐"}


def format_amount(amount: Decimal | int | str) -> str:
    """4.990 -> '4.99', 250 -> '250', 1E+2 -> '100'."""
    value = Decimal(str(amount))
    if value == value.to_integral_value():
        return str(value.quantize(Decimal(1)))
    return format(value.normalize(), "f")


def format_price(amount: Decimal | int | str, currency: str) -> str:
    sign = CURRENCY_SIGNS.get(currency)
    if sign:
        return f"{format_amount(amount)} {sign}"
    return f"{format_amount(amount)} {currency}"
