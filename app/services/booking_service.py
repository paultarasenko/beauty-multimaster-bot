import datetime as dt

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Booking,
    BookingStatus,
    Notification,
    NotificationStatus,
    NotificationType,
    User,
    WorkingDayOverride,
    WorkingHours,
)
from app.database.repositories.booking_repository import BookingRepository
from app.database.repositories.schedule_repository import ScheduleRepository
from app.database.repositories.service_repository import ServiceRepository
from app.database.repositories.user_repository import UserRepository
from app.services.availability_service import AvailabilityService, Interval
from app.utils.master_settings import (
    DEFAULT_CANCEL_STREAK_LIMIT,
    DEFAULT_MAX_ACTIVE_BOOKINGS,
    DEFAULT_SLOT_INTERVAL,
    REMINDER_24H_HOURS,
    REMINDER_2H_HOURS,
)
from app.services.limit_request_service import burn_unused_approvals, get_unused_approval
from app.services.exceptions import (
    BookingLimitReachedError,
    ClientBlockedError,
    ClientRestrictedError,
    BookingNotCancellableError,
    BookingNotFoundError,
    ServiceInactiveError,
    ServiceNotFoundError,
    SlotNotAvailableError,
)


# Значения по умолчанию (этап 8: у каждого мастера свои — master_settings.max_active_bookings / cancel_streak_limit).
MAX_ACTIVE_BOOKINGS = DEFAULT_MAX_ACTIVE_BOOKINGS  # одновременно действующих записей у одного клиента
CANCEL_STREAK_ALERT = DEFAULT_CANCEL_STREAK_LIMIT  # столько отмен подряд (самим клиентом) — повод предупредить мастера
CANCEL_STREAK_LOOKBACK = 50  # сколько последних записей смотрим, чтобы посчитать реальную длину серии


class BookingService:
    """Единая точка входа для создания/отмены/переноса записей.

    Используется и клиентскими, и админскими handlers — чтобы не дублировать
    бизнес-логику (двойной booking flow — частый источник багов, если разводить
    "клиентскую" и "админскую" версии одной и той же операции).
    """

    def __init__(
        self,
        session: AsyncSession,
        tz: dt.tzinfo,
        *,
        master_id: int,
        slot_interval_minutes: int = DEFAULT_SLOT_INTERVAL,
        reminder_hours_before: int = REMINDER_24H_HOURS,
        short_reminder_hours_before: int = REMINDER_2H_HOURS,
        max_active_bookings: int = DEFAULT_MAX_ACTIVE_BOOKINGS,
        cancel_streak_limit: int = DEFAULT_CANCEL_STREAK_LIMIT,
        reminder_24h_enabled: bool = True,
        reminder_2h_enabled: bool = True,
        master_reminder_2h_enabled: bool = True,
    ):
        """Настройки мастера (этап 8) приходят явными параметрами; значения по умолчанию = прежнее поведение.
        Из хендлеров сервис создаётся через `booking_service_for(session, tz, tenant)`."""
        self.session = session
        self.tz = tz
        self.master_id = master_id
        self.reminder_hours_before = reminder_hours_before
        self.short_reminder_hours_before = short_reminder_hours_before
        self.max_active_bookings = max_active_bookings
        self.cancel_streak_limit = cancel_streak_limit
        self.reminder_24h_enabled = reminder_24h_enabled
        self.reminder_2h_enabled = reminder_2h_enabled
        self.master_reminder_2h_enabled = master_reminder_2h_enabled
        self.availability = AvailabilityService(slot_interval_minutes=slot_interval_minutes)
        self.bookings = BookingRepository(session, master_id)
        self.services = ServiceRepository(session, master_id)
        self.schedule = ScheduleRepository(session, master_id)
        self.users = UserRepository(session, master_id)

    async def _load_availability_inputs(self, date: dt.date):
        weekday = date.weekday()
        working_hours = await self.schedule.get_working_hours_for_date(date)
        breaks = await self.schedule.get_breaks(weekday)
        day_start = dt.datetime.combine(date, dt.time.min, tzinfo=self.tz)
        day_end = dt.datetime.combine(date, dt.time.max, tzinfo=self.tz)
        blocked_periods = await self.schedule.get_blocked_periods(day_start, day_end)
        # Занято не только время услуги, но и свободное время после неё (подготовка к следующему клиенту).
        existing_bookings = await self.bookings.get_confirmed_busy_in_range(day_start, day_end)
        booking_ranges = [Interval(b.start_datetime, b.busy_end_datetime) for b in existing_bookings]
        return working_hours, breaks, blocked_periods, booking_ranges

    async def get_available_slots(
        self, date: dt.date, service_duration_minutes: int, now: dt.datetime | None = None
    ) -> list[dt.datetime]:
        working_hours, breaks, blocked_periods, booking_ranges = await self._load_availability_inputs(date)
        return self.availability.get_available_slots(
            date,
            self.tz,
            service_duration_minutes,
            working_hours,
            breaks,
            blocked_periods,
            booking_ranges,
            now,
        )

    async def has_any_slot(
        self, date: dt.date, service_duration_minutes: int, now: dt.datetime | None = None
    ) -> bool:
        working_hours, breaks, blocked_periods, booking_ranges = await self._load_availability_inputs(date)
        return self.availability.has_any_slot(
            date,
            self.tz,
            service_duration_minutes,
            working_hours,
            breaks,
            blocked_periods,
            booking_ranges,
            now,
        )

    async def dates_with_slots(
        self, dates: list[dt.date], service_duration_minutes: int, now: dt.datetime | None = None
    ) -> list[dt.date]:
        """Даты из `dates` со свободным слотом под услугу. Результат тот же, что у has_any_slot по каждой дате,
        но данные грузятся 4 запросами на весь диапазон, а не 4 запросами на дату (этап 12C: ~120 → 4 на страницу).

        `now` применяется только к первой дате списка, если она сегодняшняя (как в страницах выбора даты)."""
        if not dates:
            return []
        working_hours = await self.schedule.get_all_working_hours()
        overrides = await self.schedule.get_overrides_in_range(min(dates), max(dates))
        breaks = await self.schedule.get_all_breaks()
        range_start = dt.datetime.combine(min(dates), dt.time.min, tzinfo=self.tz)
        range_end = dt.datetime.combine(max(dates), dt.time.max, tzinfo=self.tz)
        blocked_all = await self.schedule.get_blocked_periods(range_start, range_end)
        bookings_all = await self.bookings.get_confirmed_busy_in_range(range_start, range_end)
        busy_all = [Interval(b.start_datetime, b.busy_end_datetime) for b in bookings_all]
        result: list[dt.date] = []
        for index, date in enumerate(dates):
            day_start = dt.datetime.combine(date, dt.time.min, tzinfo=self.tz)
            day_end = dt.datetime.combine(date, dt.time.max, tzinfo=self.tz)
            # Те же условия пересечения, что в SQL по одному дню: start < конец дня и end > начало дня.
            day_blocked = [p for p in blocked_all if p.start_datetime < day_end and p.end_datetime > day_start]
            day_busy = [i for i in busy_all if i.start < day_end and i.end > day_start]
            if self.availability.has_any_slot(
                date,
                self.tz,
                service_duration_minutes,
                overrides[date] if date in overrides else working_hours.get(date.weekday()),  # исключение на дату главнее недели
                breaks.get(date.weekday(), []),
                day_blocked,
                day_busy,
                now if index == 0 else None,
            ):
                result.append(date)
        return result

    async def get_day_schedule(
        self, date: dt.date
    ) -> tuple[WorkingHours | WorkingDayOverride | None, list[Interval], list[Interval]]:
        """Расписание дня целиком: рабочие часы, свободные интервалы и интервалы существующих записей.

        В отличие от get_available_slots/has_any_slot не привязан к длительности услуги —
        нужен клиенту, чтобы просто посмотреть свободное/занятое время за день.
        """
        working_hours, breaks, blocked_periods, booking_ranges = await self._load_availability_inputs(date)
        free_intervals = self.availability.get_free_intervals(
            date, self.tz, working_hours, breaks, blocked_periods, booking_ranges
        )
        return working_hours, free_intervals, booking_ranges

    async def create_booking(
        self,
        user_id: int,
        service_id: int,
        start_datetime: dt.datetime,
        manual_client_name: str | None = None,
        manual_client_contact: str | None = None,
        previous_booking_id: int | None = None,
        enforce_client_limits: bool = False,
    ) -> Booking:
        """Создаёт запись. Повторно проверяет доступность прямо перед вставкой (стейл-данные
        с экрана выбора времени не гарантируют, что слот всё ещё свободен), финальная и
        неопровержимая защита — exclusion constraint в PostgreSQL, который ловит IntegrityError
        при реальной гонке двух параллельных запросов.
        """
        # Ограничения касаются только самостоятельной записи клиента: мастер записывает без лимита,
        # а перенос не создаёт «лишней» записи (старая отменяется сразу следом).
        limit_override = None
        if enforce_client_limits and previous_booking_id is None:
            limit_override = await self.ensure_client_can_book(user_id)

        service = await self.services.get_by_id(service_id)
        if service is None:
            raise ServiceNotFoundError(f"Service {service_id} not found")
        if not service.active:
            raise ServiceInactiveError(f"Service {service_id} is not active")

        if start_datetime.tzinfo is None:
            raise TypeError("start_datetime must be timezone-aware")

        end_datetime = start_datetime + dt.timedelta(minutes=service.duration_minutes)

        # В расписании услуга занимает длительность + свободное время после неё.
        available_slots = await self.get_available_slots(
            start_datetime.date(), service.occupied_minutes, now=dt.datetime.now(self.tz)
        )
        if start_datetime not in available_slots:
            raise SlotNotAvailableError("Slot is no longer available")

        booking = self.bookings.create(
            user_id=user_id,
            service_id=service.id,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            status=BookingStatus.CONFIRMED.value,
            service_name_snapshot=service.name,
            service_price_snapshot=service.price,
            service_duration_snapshot=service.duration_minutes,
            service_buffer_snapshot=service.buffer_minutes or 0,
            manual_client_name=manual_client_name,
            manual_client_contact=manual_client_contact,
            previous_booking_id=previous_booking_id,
        )
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise SlotNotAvailableError("Slot was taken concurrently") from exc

        if limit_override is not None:
            # Разовое разрешение мастера на запись сверх лимита использовано.
            limit_override.override_used_at = dt.datetime.now(dt.timezone.utc)
            # С этого момента у клиента больше трёх записей: начинаем считать его отмены (если счёт уже идёт — не сбрасываем).
            client = await self.users.get_client(user_id)
            if client is not None and client.over_limit_cancels is None:
                client.over_limit_cancels = 0
            await self.session.flush()

        await self._schedule_reminder(booking)
        return booking

    async def ensure_client_can_book(self, user_id: int, now: dt.datetime | None = None):
        """Бросает ClientRestrictedError, если мастер ограничил доступ, и BookingLimitReachedError,
        если у клиента уже max_active_bookings (лимит мастера) действующих записей.

        Если лимит достигнут, но мастер дал разовое разрешение — возвращает этот запрос (его надо
        «погасить» после создания записи), иначе None."""
        client = await self.users.get_client(user_id)
        if client is not None and client.is_blocked_forever:
            raise ClientBlockedError(f"User {user_id} is blocked forever")
        if client is not None and client.is_restricted:
            raise ClientRestrictedError(f"User {user_id} is restricted")
        now = now or dt.datetime.now(self.tz)
        active = await self.bookings.count_active_for_user(user_id, now)
        if active >= self.max_active_bookings:
            approval = await get_unused_approval(self.session, self.master_id, user_id)
            if approval is None:
                by_master = await self.bookings.count_master_created_active_for_user(user_id, now)
                raise BookingLimitReachedError(
                    f"User {user_id} already has {active} active bookings", active=active, by_master=by_master
                )
            return approval
        # Активных записей меньше лимита (что-то выполнено или отменено): правило «не больше трёх» действует заново,
        # а неиспользованное разрешение мастера на запись сверх лимита больше не нужно и сгорает.
        if await get_unused_approval(self.session, self.master_id, user_id) is not None:
            await burn_unused_approvals(self.session, self.master_id, user_id)
        return None

    async def cancellation_streak(self, user: User) -> int:
        """Сколько последних самостоятельных записей клиента подряд отменил он сам (не больше CANCEL_STREAK_LOOKBACK).

        Серию прерывает любая запись, не отменённая клиентом (выполненная, действующая, отменённая мастером).
        """
        client = await self.users.get_client(user.id)
        recent = await self.bookings.get_recent_self_bookings(
            user.id, client.cancel_streak_reset_at if client is not None else None, limit=CANCEL_STREAK_LOOKBACK
        )
        streak = 0
        for booking in recent:
            if booking.status == BookingStatus.CANCELLED.value and booking.cancelled_by_client:
                streak += 1
            else:
                break
        return streak

    async def note_over_limit_cancellation(self, user: User, now: dt.datetime | None = None) -> int:
        """Клиент сам отменил запись: если у него было больше трёх записей, добавляет отмену в счёт.

        Возвращает, сколько отмен набралось (0 — клиент не в состоянии «больше трёх»). Когда записей снова меньше лимита,
        а трёх отмен не набралось, состояние снимается: старые отмены потом не складываются с новыми через месяцы."""
        client = await self.users.get_client(user.id)
        if client is None or client.over_limit_cancels is None:
            return 0
        client.over_limit_cancels += 1
        count = client.over_limit_cancels
        if count < self.cancel_streak_limit:
            active = await self.bookings.count_active_for_user(user.id, now or dt.datetime.now(self.tz))
            if active < self.max_active_bookings:
                client.over_limit_cancels = None
                return 0
        return count

    async def cancel_all_upcoming(self, user_id: int, now: dt.datetime | None = None) -> list[Booking]:
        """Отменяет все будущие записи клиента (освобождает слоты) и возвращает их. Отмена «от мастера»:
        в серию отмен клиента не засчитывается, напоминания по этим записям гасятся."""
        now = now or dt.datetime.now(self.tz)
        cancelled = []
        for booking in await self.bookings.get_user_upcoming(user_id, now):
            cancelled.append(await self.cancel_booking(booking.id, now, by_client=False))
        return cancelled

    async def is_cancellation_alert(self, user: User) -> bool:
        """cancel_streak_limit (порог мастера) и более отмен подряд. Сразу после предупреждения счёт начинается заново
        (cancel_streak_reset_at), поэтому мастер получает его после каждых новых N отмен, а не после каждой отмены."""
        return await self.cancellation_streak(user) >= self.cancel_streak_limit

    async def _schedule_reminder(self, booking: Booking) -> None:
        now = dt.datetime.now(self.tz)
        # Выключенное мастером напоминание не создаётся (при отправке флаг перепроверяется ещё раз — ReminderService).
        reminders = (
            (NotificationType.REMINDER_24H, self.reminder_hours_before, self.reminder_24h_enabled),
            (NotificationType.REMINDER_2H_CLIENT, self.short_reminder_hours_before, self.reminder_2h_enabled),
            (NotificationType.REMINDER_2H_MASTER, self.short_reminder_hours_before, self.master_reminder_2h_enabled),
        )
        added = False
        for notification_type, hours_before, enabled in reminders:
            if not enabled:
                continue
            reminder_at = booking.start_datetime - dt.timedelta(hours=hours_before)
            # Если запись создана позже, чем "старт - N ч" (например, мастер вписал клиента
            # на завтра), просто не планируем этот reminder — отправлять уже нечего/поздно.
            if reminder_at <= now:
                continue
            self.session.add(
                Notification(
                    master_id=self.master_id,
                    booking_id=booking.id,
                    type=notification_type.value,
                    scheduled_at=reminder_at,
                    status=NotificationStatus.PENDING.value,
                )
            )
            added = True
        if added:
            await self.session.flush()

    @staticmethod
    def is_modifiable(booking: Booking, now: dt.datetime) -> bool:
        """Запись можно отменить или перенести: она подтверждена и ещё не началась."""
        return booking.status == BookingStatus.CONFIRMED.value and booking.start_datetime >= now

    async def cancel_booking(
        self, booking_id: int, now: dt.datetime | None = None, by_client: bool = False
    ) -> Booking:
        booking = await self.bookings.get_by_id(booking_id)
        if booking is None:
            raise BookingNotFoundError(f"Booking {booking_id} not found")
        now = now or dt.datetime.now(self.tz)
        if not self.is_modifiable(booking, now):
            raise BookingNotCancellableError("Booking cannot be cancelled")

        booking.status = BookingStatus.CANCELLED.value
        booking.cancelled_by_client = by_client
        await self._cancel_pending_reminders(booking.id)
        await self.session.flush()
        return booking

    async def _cancel_pending_reminders(self, booking_id: int) -> None:
        await self.session.execute(
            update(Notification)
            .where(
                Notification.master_id == self.master_id,
                Notification.booking_id == booking_id,
                Notification.status == NotificationStatus.PENDING.value,
            )
            .values(status=NotificationStatus.CANCELLED.value)
        )

    async def reschedule_booking(
        self, booking_id: int, new_start_datetime: dt.datetime, now: dt.datetime | None = None
    ) -> Booking:
        """Перенос: сначала пытаемся создать НОВУЮ запись на новый слот. Только если это
        удалось — отменяем старую. Если новый слот занят, старая запись остаётся нетронутой
        и клиент не теряет своё время.
        """
        old_booking = await self.bookings.get_by_id(booking_id)
        if old_booking is None:
            raise BookingNotFoundError(f"Booking {booking_id} not found")
        now = now or dt.datetime.now(self.tz)
        if not self.is_modifiable(old_booking, now):
            raise BookingNotCancellableError("Booking cannot be rescheduled")

        new_booking = await self.create_booking(
            user_id=old_booking.user_id,
            service_id=old_booking.service_id,
            start_datetime=new_start_datetime,
            manual_client_name=old_booking.manual_client_name,
            manual_client_contact=old_booking.manual_client_contact,
            previous_booking_id=old_booking.id,
        )

        old_booking.status = BookingStatus.CANCELLED.value
        await self._cancel_pending_reminders(old_booking.id)
        # Непринятое приглашение переезжает на новую запись: иначе ссылка, которую мастер уже
        # отправил клиенту, привела бы к отменённой записи. Токен уникален, поэтому сначала
        # освобождаем его у старой записи отдельным flush.
        invite_token = old_booking.invite_token
        if invite_token is not None:
            old_booking.invite_token = None
            await self.session.flush()
            new_booking.invite_token = invite_token
        await self.session.flush()
        return new_booking

    async def claim_invitation(self, token: str, user_id: int, now: dt.datetime | None = None) -> Booking | None:
        """Клиент открыл ссылку-приглашение: привязывает к нему запись, созданную мастером.
        None — ссылка недействительна (уже использована, запись отменена или прошла)."""
        return await self.bookings.claim_by_invite_token(token, user_id, now or dt.datetime.now(self.tz))


async def booking_service_for(session: AsyncSession, tz: dt.tzinfo, tenant) -> BookingService:
    """BookingService с настройками мастера этого апдейта (`tenant` — TenantContext): интервал слотов, лимит записей,
    порог серии отмен, включённые напоминания. Настройки читаются один раз за апдейт (`tenant.get_settings()`)."""
    s = await tenant.get_settings()
    return BookingService(
        session,
        tz,
        master_id=tenant.master_id,
        slot_interval_minutes=s.slot_interval_minutes,
        max_active_bookings=s.max_active_bookings,
        cancel_streak_limit=s.cancel_streak_limit,
        reminder_24h_enabled=s.reminder_24h_enabled,
        reminder_2h_enabled=s.reminder_2h_enabled,
        master_reminder_2h_enabled=s.master_reminder_2h_enabled,
    )


async def complete_past_bookings(session: AsyncSession, master_id: int, now: dt.datetime) -> int:
    """Переводит все подтверждённые брони мастера, у которых уже прошло end_datetime, в статус
    "выполнена". Массовый UPDATE прямо в БД, а не построчная загрузка — дешевле и без
    гонок: условие WHERE status='confirmed' AND end_datetime <= now само по себе идемпотентно
    (повторный запуск ничего не сломает — уже завершённые брони под него не попадают).
    """
    result = await session.execute(
        update(Booking)
        .where(
            Booking.master_id == master_id,
            Booking.status == BookingStatus.CONFIRMED.value,
            Booking.end_datetime <= now,
        )
        .values(status=BookingStatus.COMPLETED.value)
    )
    await session.commit()
    return result.rowcount
