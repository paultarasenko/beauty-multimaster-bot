from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database.models import MasterClient, User
from app.database.repositories.base import MasterScopedRepository


class UserRepository(MasterScopedRepository):
    """Клиенты одного мастера.

    `users` — глобальная личность по telegram_id (один человек может быть клиентом нескольких мастеров), а «клиент
    мастера» — это пара (master_id, user_id) в `master_clients` вместе с ограничениями доступа. Все методы чтения
    видят только тех пользователей, которые связаны с `master_id` этого репозитория.
    """

    def _select_clients(self):
        """SELECT пользователей, привязанных к этому мастеру."""
        return (
            select(User)
            .join(MasterClient, MasterClient.user_id == User.id)
            .where(MasterClient.master_id == self.master_id)
        )

    async def _get_any_by_telegram_id(self, telegram_id: int) -> User | None:
        """Пользователь по telegram_id среди ВСЕХ людей (для создания/привязки). Наружу не отдаётся."""
        result = await self.session.execute(select(User).where(User.telegram_id == telegram_id))
        return result.scalar_one_or_none()

    async def _link_to_master(self, user: User) -> None:
        """Пользователь становится клиентом этого мастера (строка master_clients). Повторный вызов безопасен."""
        await self.session.execute(
            pg_insert(MasterClient)
            .values(master_id=self.master_id, user_id=user.id)
            .on_conflict_do_nothing(constraint="uq_master_clients_master_user")
        )
        await self.session.flush()

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        result = await self.session.execute(self._select_clients().where(User.telegram_id == telegram_id))
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> User | None:
        """Клиент этого мастера по id. Чужой клиент (или несуществующий id) — None."""
        result = await self.session.execute(self._select_clients().where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_client(self, user_id: int) -> MasterClient | None:
        """Связь «клиент мастера» с ограничениями доступа. None — человек не клиент этого мастера."""
        result = await self.session.execute(
            select(MasterClient).where(MasterClient.master_id == self.master_id, MasterClient.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> User | None:
        """Клиент этого мастера с таким username (без учёта регистра, как в Telegram).

        Сравнение точное: `ilike` здесь нельзя — в нём `_` и `%` это подстановочные символы, а `_` в username
        встречается постоянно (`anna_k` совпало бы с `annaxk`).

        username в БД не уникален: если человек сменил ник, а ник занял другой, у старой строки он остаётся до
        её следующего обновления. Поэтому при нескольких совпадениях берём самую свежо обновлённую строку,
        а не падаем с MultipleResultsFound.
        """
        result = await self.session.execute(
            self._select_clients()
            .where(func.lower(User.username) == username.lower())
            .order_by(User.updated_at.desc(), User.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_or_create(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> User:
        """Вызывается на каждый /start. Не создаёт дубликаты, обновляет данные если юзер уже есть.

        Если человек уже известен системе (клиент другого мастера), заводится только связь с этим мастером.
        """
        user = await self._get_any_by_telegram_id(telegram_id)
        if user is None:
            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
            self.session.add(user)
            await self.session.flush()
            await self._link_to_master(user)
            return user

        user.username = username
        user.first_name = first_name
        user.last_name = last_name
        await self.session.flush()
        await self._link_to_master(user)
        return user

    async def set_admin_panel_mode(self, telegram_id: int, value: bool) -> None:
        """Персистентно сохраняет, в каком меню сейчас владелец этого мастера (панель мастера / клиентский режим).

        Флаг хранится в master_clients (у связи человека с этим мастером), поэтому у человека, который владелец
        мастера A и клиент мастера B, режимы в двух ботах независимы.

        Вызывается и до первого /start (например, если админ сразу отправил /admin), поэтому
        сам создаёт строку пользователя и связь с мастером при необходимости — без имени/username, они допишутся
        следующим /start через get_or_create.
        """
        user = await self._get_any_by_telegram_id(telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id)
            self.session.add(user)
            await self.session.flush()
        await self._link_to_master(user)
        client = await self.get_client(user.id)
        client.is_in_admin_panel = value
        await self.session.flush()

    async def is_in_admin_panel(self, telegram_id: int) -> bool:
        result = await self.session.execute(
            select(MasterClient.is_in_admin_panel)
            .join(User, User.id == MasterClient.user_id)
            .where(MasterClient.master_id == self.master_id, User.telegram_id == telegram_id)
        )
        return bool(result.scalar_one_or_none())
