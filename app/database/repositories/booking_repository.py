import datetime as dt

from sqlalchemy import func, select, update

from app.database.models import Booking, BookingStatus, User
from app.database.repositories.base import MasterScopedRepository


class BookingRepository(MasterScopedRepository):

    async def get_confirmed_in_range(
        self, range_start: dt.datetime, range_end: dt.datetime
    ) -> list[Booking]:
        result = await self.session.execute(
            select(Booking).where(
                Booking.master_id == self.master_id,
                Booking.status == BookingStatus.CONFIRMED.value,
                Booking.start_datetime < range_end,
                Booking.end_datetime > range_start,
            )
        )
        return list(result.scalars().all())

    async def get_confirmed_busy_in_range(
        self, range_start: dt.datetime, range_end: dt.datetime
    ) -> list[Booking]:
        """Как get_confirmed_in_range, но учитывает и свободное время после услуги: запись, которая
        закончилась до range_start, но её «хвост» (подготовка к следующему клиенту) заходит в диапазон,
        тоже возвращается."""
        result = await self.session.execute(
            select(Booking).where(
                Booking.master_id == self.master_id,
                Booking.status == BookingStatus.CONFIRMED.value,
                Booking.start_datetime < range_end,
                Booking.end_datetime + func.make_interval(0, 0, 0, 0, 0, Booking.service_buffer_snapshot) > range_start,
            )
        )
        return list(result.scalars().all())

    async def get_confirmed_upcoming(self, now: dt.datetime) -> list[Booking]:
        """Все подтверждённые записи начиная с «сейчас» и далее — без верхней границы.

        Нужно, чтобы найти конфликты при переносе дня недели в выходной: сам день недели
        не привязан к одной дате, поэтому конфликтующие записи нужно искать среди вообще
        всех будущих записей, а не в одном диапазоне дат.
        """
        result = await self.session.execute(
            select(Booking)
            .where(
                Booking.master_id == self.master_id,
                Booking.status == BookingStatus.CONFIRMED.value,
                Booking.start_datetime >= now,
            )
            .order_by(Booking.start_datetime)
        )
        return list(result.scalars().all())

    async def count_active_for_user(self, user_id: int, now: dt.datetime) -> int:
        """Сколько у клиента действующих записей (подтверждены и ещё не начались).

        Выполненные и отменённые не считаются — они освобождают место под новую запись. Записи, которые мастер
        создал клиенту вручную, тоже не в счёт: лимит ограничивает самостоятельные записи клиента.
        """
        result = await self.session.execute(
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.master_id == self.master_id,
                Booking.user_id == user_id,
                Booking.status == BookingStatus.CONFIRMED.value,
                Booking.start_datetime >= now,
                Booking.manual_client_name.is_(None),
            )
        )
        return result.scalar_one()

    async def count_master_created_active_for_user(self, user_id: int, now: dt.datetime) -> int:
        """Сколько у клиента действующих записей, которые мастер создал вручную (в лимит они не входят).

        Условия те же, что в count_active_for_user, но manual_client_name заполнен: вместе эти два числа дают всё,
        что клиент видит в «Мои записи».
        """
        result = await self.session.execute(
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.master_id == self.master_id,
                Booking.user_id == user_id,
                Booking.status == BookingStatus.CONFIRMED.value,
                Booking.start_datetime >= now,
                Booking.manual_client_name.is_not(None),
            )
        )
        return result.scalar_one()

    async def count_completed_for_user(self, user_id: int) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.master_id == self.master_id,
                Booking.user_id == user_id,
                Booking.status == BookingStatus.COMPLETED.value,
            )
        )
        return result.scalar_one()

    async def get_recent_self_bookings(
        self, user_id: int, since: dt.datetime | None, limit: int
    ) -> list[Booking]:
        """Последние самостоятельные записи клиента, новые первыми.

        Не попадают: записи, созданные мастером вручную, и записи, замененные переносом (у переноса «живой»
        считается последняя запись цепочки, иначе перенос выглядел бы как отмена).
        """
        replaced = select(Booking.previous_booking_id).where(
            Booking.master_id == self.master_id, Booking.previous_booking_id.is_not(None)
        )
        query = select(Booking).where(
            Booking.master_id == self.master_id,
            Booking.user_id == user_id,
            Booking.manual_client_name.is_(None),
            Booking.id.not_in(replaced),
        )
        if since is not None:
            query = query.where(Booking.created_at > since)
        result = await self.session.execute(query.order_by(Booking.created_at.desc(), Booking.id.desc()).limit(limit))
        return list(result.scalars().all())

    async def get_by_id(self, booking_id: int) -> Booking | None:
        """Запись только своего мастера: id чужой записи (из подделанной callback_data) даёт None."""
        result = await self.session.execute(
            select(Booking).where(Booking.master_id == self.master_id, Booking.id == booking_id)
        )
        return result.scalar_one_or_none()

    async def get_owned_by_telegram_id(self, booking_id: int, telegram_id: int) -> Booking | None:
        """Запись только если она принадлежит пользователю с этим telegram_id (иначе None).

        booking_id приходит из callback_data, которую клиент Telegram может подделать, поэтому любое действие
        клиента над записью по id обязано идти через эту проверку, а не через get_by_id.
        """
        result = await self.session.execute(
            select(Booking)
            .join(User, User.id == Booking.user_id)
            .where(
                Booking.master_id == self.master_id, Booking.id == booking_id, User.telegram_id == telegram_id
            )
        )
        return result.scalar_one_or_none()

    async def get_user_upcoming(self, user_id: int, now: dt.datetime) -> list[Booking]:
        result = await self.session.execute(
            select(Booking)
            .where(
                Booking.master_id == self.master_id,
                Booking.user_id == user_id,
                Booking.status == BookingStatus.CONFIRMED.value,
                Booking.start_datetime >= now,
            )
            .order_by(Booking.start_datetime)
        )
        return list(result.scalars().all())

    async def claim_by_invite_token(self, token: str, user_id: int, now: dt.datetime) -> Booking | None:
        """Привязывает запись с этим токеном к пользователю и обнуляет токен (одноразовость).

        Один атомарный UPDATE ... WHERE: если два человека откроют ссылку одновременно, запись
        достанется только одному. Отменённая или уже прошедшая запись не привязывается.
        """
        result = await self.session.execute(
            update(Booking)
            .where(
                Booking.master_id == self.master_id,
                Booking.invite_token == token,
                Booking.status == BookingStatus.CONFIRMED.value,
                Booking.start_datetime >= now,
            )
            .values(user_id=user_id, invite_token=None)
            .returning(Booking.id)
        )
        booking_id = result.scalar_one_or_none()
        if booking_id is None:
            return None
        # populate_existing — в сессии мог остаться объект со старым user_id.
        return await self.session.get(Booking, booking_id, populate_existing=True)  # booking_id получен с фильтром по мастеру

    def create(self, **kwargs) -> Booking:
        booking = Booking(master_id=self.master_id, **kwargs)
        self.session.add(booking)
        return booking
