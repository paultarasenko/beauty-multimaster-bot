class SlotNotAvailableError(Exception):
    """Слот занят / уже недоступен на момент подтверждения записи."""


class ServiceNotFoundError(Exception):
    pass


class ServiceInactiveError(Exception):
    pass


class BookingNotFoundError(Exception):
    pass


class BookingNotCancellableError(Exception):
    """Запись уже отменена/завершена/в прошлом — её нельзя отменить или перенести."""


class ReminderInPastError(Exception):
    """Одноразовое напоминание назначено на время, которое уже прошло."""



class BookingLimitReachedError(Exception):
    """У клиента уже максимум активных записей — новую создать нельзя.

    active — сколько самостоятельных активных записей у клиента сейчас (может быть больше лимита: запись сверх лимита
    по разрешению мастера), чтобы показать клиенту реальное число.
    by_master — сколько ещё действующих записей создал мастер вручную: в лимит они не входят, но клиент видит их
    в «Мои записи», поэтому в сообщении о лимите о них говорится отдельно."""

    def __init__(self, message: str = "", active: int | None = None, by_master: int = 0) -> None:
        super().__init__(message)
        self.active = active
        self.by_master = by_master


class ClientRestrictedError(Exception):
    """Мастер ограничил клиенту доступ к записи."""


class ClientBlockedError(Exception):
    """Мастер ограничил клиенту доступ навсегда: ни записи, ни обращений, ни отзывов."""
