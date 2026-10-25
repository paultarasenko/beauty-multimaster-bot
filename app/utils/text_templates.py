"""Индивидуальные тексты мастера (этап 8): безопасные шаблоны с ограниченным набором подстановок.

Безопасность:
- подстановка — только `{имя}` из белого списка ALLOWED_FIELDS; никакого str.format/Jinja/eval: шаблон разбирается
  регулярным выражением, значения подставляются как готовые строки (атрибуты, индексы, форматные спецификации
  вида `{service.__class__}`, `{0}`, `{service!r}` не поддерживаются и отвергаются);
- и значения подстановок, и «обычный» текст шаблона экранируются под HTML-разметку Telegram (бот шлёт parse_mode=HTML),
  поэтому мастер не может вставить теги, а клиент со своим именем «<b>» — сломать сообщение;
- неизвестные подстановки, лишние фигурные скобки и слишком длинный текст отвергаются при сохранении; шаблон, который
  всё же оказался некорректным (например, список полей изменился), при отправке НЕ роняет бота — используется
  текст по умолчанию.
"""

import logging
import re
from collections.abc import Callable, Mapping
from html import escape

from app.utils.master_settings import MAX_TEMPLATE_LEN

logger = logging.getLogger(__name__)

# Разрешённые подстановки (одинаковы для всех текстов) и их описание для мастера.
ALLOWED_FIELDS = {
    "client_name": "имя клиента",
    "service": "название услуги",
    "date": "дата, например 18.09.2026",
    "weekday": "день недели",
    "time": "время, например 15:00 - 16:00",
    "price": "стоимость",
    "currency": "валюта",
    "address": "адрес мастера",
    "master_name": "имя мастера",
}

_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
_NAME_RE = re.compile(r"^[a-z_]+$")
MAX_RENDERED_LEN = 4000  # лимит Telegram на сообщение — 4096

# Пример данных для предпросмотра (реальные данные не нужны: это демонстрация формата).
EXAMPLE_VALUES = {
    "client_name": "Анна",
    "service": "Маникюр классический",
    "date": "18.09.2026",
    "weekday": "Пятница",
    "time": "15:00 - 16:00",
    "price": "40",
    "currency": "BYN",
    "address": "ул. Примерная, 1",
    "master_name": "Мария",
}


class TemplateError(ValueError):
    """Шаблон не принят; текст безопасен для показа мастеру."""


def fields_help() -> str:
    """Список подстановок для подсказки мастеру."""
    return "\n".join(f"{{{name}}} — {description}" for name, description in ALLOWED_FIELDS.items())


def validate_template(text: str | None) -> str:
    """Проверяет шаблон и возвращает его без пробелов по краям. TemplateError — с понятным объяснением."""
    template = (text or "").strip()
    if not template:
        raise TemplateError("Текст пустой. Чтобы вернуть стандартный текст, нажмите «Сбросить».")
    if len(template) > MAX_TEMPLATE_LEN:
        raise TemplateError(f"Слишком длинный текст: {len(template)} символов, максимум {MAX_TEMPLATE_LEN}.")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in template):
        raise TemplateError("В тексте есть недопустимые управляющие символы.")
    unknown = []
    for match in _PLACEHOLDER_RE.finditer(template):
        name = match.group(1)
        if not _NAME_RE.match(name) or name not in ALLOWED_FIELDS:
            unknown.append("{" + name + "}")
    if unknown:
        shown = ", ".join(dict.fromkeys(unknown))
        if len(shown) > 200:
            shown = shown[:200] + "…"
        raise TemplateError(f"Неизвестная подстановка: {shown}.\n\nДоступные подстановки:\n{fields_help()}")
    leftover = _PLACEHOLDER_RE.sub("", template)
    if "{" in leftover or "}" in leftover:
        raise TemplateError(
            "Фигурные скобки допускаются только в подстановках вида {service}. Уберите лишние «{» и «}».\n\n"
            f"Доступные подстановки:\n{fields_help()}"
        )
    return template


def _render(template: str, values: Mapping[str, object]) -> str:
    """Подстановка по уже проверенному шаблону. Всё экранируется под HTML."""
    result: list[str] = []
    position = 0
    for match in _PLACEHOLDER_RE.finditer(template):
        result.append(escape(template[position : match.start()]))
        value = values.get(match.group(1))
        result.append(escape("" if value is None else str(value)))
        position = match.end()
    result.append(escape(template[position:]))
    return "".join(result)


def render_template(
    template: str | None,
    values: Mapping[str, object],
    default: Callable[[], str],
    *,
    context: str = "text",
) -> str:
    """Готовый HTML-текст сообщения: по шаблону мастера или (шаблона нет / он некорректен) по умолчанию.

    `default` — функция, возвращающая стандартный текст (вызывается только при необходимости).
    """
    if template is None:
        return default()
    try:
        validated = validate_template(template)
        text = _render(validated, values)
    except TemplateError:
        logger.warning("Индивидуальный текст «%s» некорректен: используется текст по умолчанию", context)
        return default()
    if len(text) > MAX_RENDERED_LEN:
        logger.warning("Индивидуальный текст «%s» после подстановки слишком длинный: используется текст по умолчанию", context)
        return default()
    return text


def render_preview(template: str, default_text: str | None = None) -> str:
    """Предпросмотр шаблона на примере данных (шаблон должен быть уже проверен validate_template)."""
    return _render(validate_template(template), EXAMPLE_VALUES)
