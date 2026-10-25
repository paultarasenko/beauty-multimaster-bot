import datetime as dt

from aiogram.filters.callback_data import CallbackData


class ServiceCallback(CallbackData, prefix="svc"):
    service_id: int


class DateCallback(CallbackData, prefix="date"):
    date: str  # ISO format YYYY-MM-DD


class DatesPageCallback(CallbackData, prefix="dpg"):
    page: int  # номер страницы дат: 0 — ближайшие дни, 1 — следующие и т.д.


class CalendarCallback(CallbackData, prefix="cal"):
    """Календарь «Проверить время». year/month нужны только для action=\"month\", в остальных действиях они 0."""

    action: str  # "month" — перейти на месяц | "busy" — день занят | "off" — выходной | "noop" — ничего | "cancel" — закрыть
    year: int = 0
    month: int = 0


class TimeCallback(CallbackData, prefix="time"):
    ts: int  # unix timestamp (seconds) — avoids ':' which CallbackData uses as separator


class ConfirmCallback(CallbackData, prefix="confirm"):
    action: str  # "yes" | "edit"


class ScheduleBookCallback(CallbackData, prefix="sbk"):
    """Кнопки «Записаться» под расписанием дня («Проверить время»): date — день, который смотрел клиент."""

    action: str  # "ask" — спросить «Другой день / Этот день» | "day" — записаться на этот день
    date: str  # ISO YYYY-MM-DD


class BookingActionCallback(CallbackData, prefix="booking"):
    booking_id: int
    action: str  # "cancel" | "reschedule" | "cancel_confirm" | "cancel_back"


class ReminderActionCallback(CallbackData, prefix="reminder"):
    booking_id: int
    action: str  # "still_valid" | "reschedule" | "cancel"


class NavCallback(CallbackData, prefix="nav"):
    target: str  # "main_menu" | "back"


class PortfolioCallback(CallbackData, prefix="pf"):
    item_id: int  # какое фото показать (используется и клиентом, и мастером для листания ◀️ ▶️)


class AdminPortfolioCallback(CallbackData, prefix="apf"):
    item_id: int  # 0 — не используется (action="add")
    action: str  # "add" | "delete" | "delete_confirm" | "edit_title" | "edit_description" | "edit_cancel"


class AdminBookingCallback(CallbackData, prefix="abk"):
    booking_id: int
    action: str  # "delete" | "confirm" | "back"
    # Страница списка «Отменить запись», с которой открыта запись: «Назад» возвращает на неё.
    page: int = 0

    @classmethod
    def unpack(cls, value: str) -> "AdminBookingCallback":
        # aiogram требует точного числа полей, а кнопки без страницы («abk:<id>:<action>»), отправленные до
        # появления пагинации, ещё висят в чатах: дописываем им страницу 0, чтобы они не «молчали».
        if value.count(cls.__separator__) == 2:
            value += f"{cls.__separator__}0"
        return super().unpack(value)


class AdminBookingsPageCallback(CallbackData, prefix="abkp"):
    page: int  # страница списка «Отменить запись»: 0 — ближайшие записи, 1 — следующие и т.д.


class AdminBlockCallback(CallbackData, prefix="abl"):
    block_id: int
    action: str  # "unblock" | "confirm" | "back"


class AdminScheduleCallback(CallbackData, prefix="asch"):
    action: str  # "day_off" | "lunch" | "lunch_set" | "lunch_remove" | "lunch_day" | "lunch_all" | "sd_open" | "sd_close" | "sd_reset"


class AdminServiceCallback(CallbackData, prefix="asv"):
    service_id: int
    action: str  # "open" | "list" | "toggle" | "rename" | "price" | "buffer" | "delete" | "delete_yes"


class AdminDurationCallback(CallbackData, prefix="adur"):
    service_id: int  # 0 — длительность выбирают при добавлении новой услуги
    minutes: int


class AdminBufferCallback(CallbackData, prefix="abuf"):
    service_id: int
    minutes: int  # свободное время после услуги, мин (0 — без свободного времени)


class AdminClientAccessCallback(CallbackData, prefix="acl"):
    user_id: int  # внутренний id клиента (users.id)
    action: str  # "restrict" — ограничить доступ | "allow" — подтвердить доступ


class AdminAboutCallback(CallbackData, prefix="aab"):
    action: str  # "edit" | "cancel" | "name_edit" | "name_delete"
    field: str  # "name" | "specialization" | "experience" | "description" | "none"


class AdminReminderCallback(CallbackData, prefix="arm"):
    action: str  # "new" | "cancel" | "menu" | "list" | "open" | "delete" | "confirm"
    recurrence: str  # для action="new": once | daily | weekly | monthly | yearly; иначе "none"
    reminder_id: int  # 0 — не используется


class ReportCallback(CallbackData, prefix="rep"):
    period: str  # "today" | "week" | "month" | "3months" | "6months"


class ReviewsCallback(CallbackData, prefix="rv"):
    action: str  # "view" | "add" | "edit" | "back"


class ReviewsPageCallback(CallbackData, prefix="rvpg"):
    offset: int  # смещение для следующей порции отзывов ("Читать ещё")


class ReviewRatingCallback(CallbackData, prefix="rvrt"):
    rating: int  # 1..5


class AdminReviewsCallback(CallbackData, prefix="arv"):
    action: str  # "back" — назад к меню «Посмотреть/Оставить/Удалить/Назад»


class AdminReviewDeleteCallback(CallbackData, prefix="arvd"):
    review_id: int  # 0 — не используется (для action="list")
    action: str  # "list" | "open" | "confirm"


def parse_iso_datetime(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


class MasterRequestCallback(CallbackData, prefix="mrq"):
    """Запрос клиента к мастеру на запись сверх лимита."""

    action: str  # "write" — клиент хочет написать мастеру | "cancel" — передумал


class AdminLimitRequestCallback(CallbackData, prefix="alr"):
    """Решение мастера по запросу клиента на запись сверх лимита."""

    request_id: int
    action: str  # "approve" — принять | "reject" — отклонить


class AdminLimitCommentCallback(CallbackData, prefix="alc"):
    """Ответ мастера на вопрос «Желаете добавить сообщение?»."""

    request_id: int
    action: str  # "yes" | "no"


class AdminSettingsCallback(CallbackData, prefix="ast"):
    """Экран «⚙️ Настройки» мастера (этап 8)."""

    action: str  # "menu" | "set" | "toggle" | "currency" | "text" | "text_edit" | "text_reset" | "cancel" | "quiet"
    field: str  # имя настройки / раздела меню; "none" — не используется
    value: str  # новое значение для action="set"; иначе "none"


class AdminAccessRequestCallback(CallbackData, prefix="aar"):
    """Решение мастера по обращению ограниченного клиента."""

    request_id: int
    action: str  # "open" — открыть доступ | "block" — ограничить навсегда


class MyBotCallback(CallbackData, prefix="mbt"):
    """Раздел «🤖 Мой бот» мастера (этап 9): только просмотр и проверка."""

    action: str  # "show" | "check" | "webhook"
