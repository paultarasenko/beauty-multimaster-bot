"""Форматирование длительности для текстов бота."""


def format_duration(total_minutes: int) -> str:
    """Длительность в минутах -> «45 мин», «1 ч», «1 ч 25 мин».

    В БД длительность хранится в минутах, здесь меняется только отображение.
    0 -> «0 мин»; нечисловое значение возвращается как есть.
    """
    try:
        total = int(total_minutes)
    except (TypeError, ValueError):
        return str(total_minutes)
    hours, minutes = divmod(max(total, 0), 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"
