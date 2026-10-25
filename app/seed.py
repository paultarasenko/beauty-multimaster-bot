"""Первичное заполнение БД тестовыми данными.

Запуск: python -m app.seed

Заполняет мастера из настройки MASTER_ID (по умолчанию 1) — он должен уже существовать (мастер №1 создаёт миграция).

Не запускается автоматически при старте приложения — только руками, один раз
(или повторно, идемпотентно проверяет наличие данных перед вставкой).
"""

import asyncio
import datetime as dt

from sqlalchemy import select

from app.config.settings import settings
from app.database.models import Master, MasterProfile, Service, WorkingHours
from app.database.repositories.master_settings_repository import MasterSettingsRepository
from app.database.session import async_session_factory


async def seed(master_id: int | None = None) -> None:
    master_id = settings.master_id if master_id is None else master_id
    async with async_session_factory() as session:
        if await session.get(Master, master_id) is None:
            raise SystemExit(f"Мастер id={master_id} не найден: сначала примените миграции (alembic upgrade head).")

        existing_profile = (
            await session.execute(select(MasterProfile).where(MasterProfile.master_id == master_id))
        ).scalar_one_or_none()
        if existing_profile is None:
            session.add(
                MasterProfile(
                    master_id=master_id,
                    name="Анна Иванова",
                    description=(
                        "Маникюр, в котором красота начинается с деталей.\n\n"
                        "Руки говорят о женщине не меньше, чем взгляд и улыбка. Поэтому здесь каждая мелочь на своём месте: "
                        "бережно ухоженная кутикула, безупречная форма ногтя, глубокий цвет и покрытие, которое остаётся "
                        "свежим и сияющим от первого дня до последнего.\n\n"
                        "Дизайн создаётся лично для вас. Он учитывает форму ваших рук, ваш ритм жизни и характер: "
                        "от шёлкового нюда и тонкой геометрии до смелых акцентов, на которые хочется смотреть снова и снова.\n\n"
                        "Только качественные материалы, безупречная чистота инструментов и внимание к каждой детали. "
                        "Результат, которым хочется любоваться и который приятно показывать.\n\n"
                        "Приходите за руками, которые будут радовать вас каждое утро ✨"
                    ),
                    address="ул. Лобанка, 42, Минск",
                    location_landmark="Рядом с ТЦ Origo",
                    contact_username="anna_beauty",
                    experience_years=5,
                    specialization="Маникюр, покрытие гель-лак, дизайн ногтей",
                    timezone="Europe/Minsk",
                )
            )
            print("✓ master_profile создан")
        else:
            print("• master_profile уже существует, пропускаю")

        await MasterSettingsRepository(session, master_id).ensure_row()  # настройки по умолчанию (этап 8)

        existing_services = (
            await session.execute(select(Service).where(Service.master_id == master_id))
        ).scalars().first()
        if existing_services is None:
            session.add_all(
                [
                    Service(
                        master_id=master_id,
                        name="Маникюр классический",
                        description="Классический маникюр без покрытия.",
                        price=25,
                        duration_minutes=45,
                        active=True,
                        sort_order=0,
                    ),
                    Service(
                        master_id=master_id,
                        name="Маникюр + гель-лак",
                        description="Маникюр с покрытием гель-лаком.",
                        price=40,
                        duration_minutes=60,
                        active=True,
                        sort_order=1,
                    ),
                    Service(
                        master_id=master_id,
                        name="Наращивание ногтей",
                        description="Наращивание с дизайном.",
                        price=60,
                        duration_minutes=90,
                        active=True,
                        sort_order=2,
                    ),
                ]
            )
            print("✓ тестовые услуги добавлены (3 шт.)")
        else:
            print("• услуги уже существуют, пропускаю")

        existing_wh = (
            await session.execute(select(WorkingHours).where(WorkingHours.master_id == master_id))
        ).scalars().first()
        if existing_wh is None:
            # Пн-Пт 10:00-19:00, Сб 10:00-15:00, Вс выходной
            for weekday in range(5):
                session.add(
                    WorkingHours(
                        master_id=master_id,
                        weekday=weekday,
                        is_working=True,
                        start_time=dt.time(10, 0),
                        end_time=dt.time(19, 0),
                    )
                )
            session.add(
                WorkingHours(
                    master_id=master_id, weekday=5, is_working=True, start_time=dt.time(10, 0), end_time=dt.time(15, 0)
                )
            )
            session.add(WorkingHours(master_id=master_id, weekday=6, is_working=False, start_time=None, end_time=None))
            print("✓ базовое расписание добавлено (Пн-Пт 10-19, Сб 10-15, Вс выходной)")
        else:
            print("• расписание уже существует, пропускаю")

        await session.commit()
    print("\nГотово.")


if __name__ == "__main__":
    asyncio.run(seed())
