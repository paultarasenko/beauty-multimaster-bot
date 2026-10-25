"""Тесты-сторожа этапа 5: незащищённый админский хендлер и запрос без master_id не пройдут CI.

Сторожа проверяют сами себя на заведомо плохих примерах — иначе «зелёный» сторож мог бы просто ничего не искать.
"""

import importlib
import pkgutil
from inspect import Parameter, signature

import pytest
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

import app.bot.handlers.admin as admin_package
import app.database.repositories as repositories_package
from app.bot.menu_mode import is_admin_mode
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.database.repositories.base import MasterIdRequiredError, MasterScopedRepository
from app.tenancy import TenantContext
from tests import static_guards as sg
from tests.conftest import set_owner

# Админские модули, чьи обработчики ОБЩИЕ с клиентом (кнопки меню есть у обоих). Фильтра владельца на роутере нет;
# админская ветка внутри них включается только через is_admin_mode (проверка владельца), что проверяет тест ниже.
SHARED_ADMIN_MODULES = {"content": "кнопки «Услуги/Портфолио/О мастере/Отзывы/Связь» есть и у клиента, и у мастера"}


def _is_owner_filter(filter_object) -> bool:
    return isinstance(filter_object.callback, IsAdminFilter)


def unguarded_handlers(router: Router) -> list[str]:
    """Хендлеры роутера без IsAdminFilter ни на уровне роутера (для события), ни на самом хендлере. Любые события."""
    problems = []
    for event_name, observer in router.observers.items():
        router_level = any(_is_owner_filter(f) for f in observer._handler.filters)
        for handler in observer.handlers:
            handler_level = any(_is_owner_filter(f) for f in (handler.filters or []))
            if not (router_level or handler_level):
                problems.append(f"{event_name}:{handler.callback.__name__}")
    return problems


def _admin_routers() -> list[tuple[str, Router]]:
    found = []
    for module_info in pkgutil.iter_modules(admin_package.__path__):
        module = importlib.import_module(f"{admin_package.__name__}.{module_info.name}")
        router = getattr(module, "router", None)
        assert isinstance(router, Router), f"admin/{module_info.name}.py: нет router"
        found.append((module_info.name, router))
    return found


# ---------- сторож админских хендлеров ----------


def test_admin_package_is_not_empty():
    assert len(_admin_routers()) >= 12


@pytest.mark.parametrize("name,router", _admin_routers(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_admin_handler_checks_owner(name, router):
    """Каждый хендлер (Message и CallbackQuery и любые другие события) закрыт фильтром владельца текущего мастера."""
    if name in SHARED_ADMIN_MODULES:
        pytest.skip(f"общий модуль: {SHARED_ADMIN_MODULES[name]}")
    problems = unguarded_handlers(router)
    assert not problems, (
        f"admin/{name}.py: хендлеры без IsAdminFilter: {problems}. Добавьте router.message.filter(IsAdminFilter()) и "
        "router.callback_query.filter(IsAdminFilter()) или фильтр на сам хендлер."
    )


def test_guard_detects_unprotected_handlers():
    """Самопроверка сторожа: хендлер без фильтра ловится, защищённый на роутере и на хендлере — нет."""
    bad = Router(name="bad")

    @bad.callback_query(F.data == "x")
    async def naked(callback: CallbackQuery): ...

    assert unguarded_handlers(bad) == ["callback_query:naked"]

    message_only = Router(name="half")
    message_only.message.filter(IsAdminFilter())

    @message_only.message(F.text == "a")
    async def guarded_message(message: Message): ...

    @message_only.callback_query(F.data == "b")  # фильтр стоит только на message — callback открыт
    async def open_callback(callback: CallbackQuery): ...

    assert unguarded_handlers(message_only) == ["callback_query:open_callback"]

    per_handler = Router(name="per_handler")

    @per_handler.message(F.text == "a", IsAdminFilter())
    async def guarded(message: Message): ...

    assert unguarded_handlers(per_handler) == []


def test_shared_admin_handlers_use_owner_aware_admin_mode():
    """В общих хендлерах админская ветка включается только через is_admin_mode (владелец + режим панели).

    Флаг is_in_admin_panel живёт в БД у человека и переживает смену владельца: напрямую его использует только menu_mode.
    """
    offenders = []
    for path in sg.app_python_files():
        rel = sg._rel(path)
        # Слой хендлеров/middleware; menu_mode — обёртка над хранилищем, репозиторий — само хранилище флага.
        if not rel.startswith("app/bot/") or rel == "app/bot/menu_mode.py":
            continue
        if "is_in_admin_panel(" in path.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert not offenders, f"прямой вызов is_in_admin_panel вне menu_mode: {offenders} — используйте is_admin_mode"


async def test_ex_owner_with_stale_panel_flag_is_not_in_admin_mode(session):
    from app.bot.menu_mode import enter_admin_panel
    from app.database.models import Master

    await set_owner(session, 900)
    await enter_admin_panel(session, 1, 900)
    await session.commit()
    master = await session.get(Master, 1)
    tenant = TenantContext(master_id=1, master=master)
    assert await is_admin_mode(session, tenant, 900) is True

    await set_owner(session, 901)  # владельца сменили: у 900 в БД остался флаг панели
    session.expire_all()
    master = await session.get(Master, 1)
    tenant = TenantContext(master_id=1, master=master)
    assert await is_admin_mode(session, tenant, 900) is False
    assert await is_admin_mode(session, tenant, 901) is False  # новый владелец панель ещё не открывал


# ---------- сторож прямых запросов ----------


def test_no_unscoped_queries_models_repositories_or_module_state_in_app():
    findings = sg.scan_app()
    assert not findings, "Нарушения изоляции:\n" + "\n".join(str(f) for f in findings)


def test_tenant_models_are_discovered():
    models = sg.tenant_models()
    assert {"Booking", "Service", "Review", "PortfolioItem", "MasterClient", "AdminReminder", "Notification"} <= models
    assert "User" not in models and "Master" not in models  # глобальная личность и сам арендатор


def test_query_guard_detects_bad_and_accepts_good_code():
    models = sg.tenant_models()
    bad = (
        "async def f(session):\n"
        "    await session.execute(select(Booking).where(Booking.id == 1))\n"
        "    await session.execute(select(func.count()).select_from(Booking))\n"
        "    await session.execute(update(Notification).where(Notification.id == 1).values(status='x'))\n"
        "    await session.execute(delete(Review).where(Review.id == 5))\n"
        "    await session.get(Service, 5)\n"
    )
    found = sg.find_unscoped_queries(bad, "x.py", models)
    assert sorted(f.line for f in found) == [2, 3, 4, 5, 6]

    good = (
        "async def f(session, m):\n"
        "    await session.execute(select(Booking, User).join(User, User.id == Booking.user_id).where(Booking.master_id == m))\n"
        "    await session.execute(select(func.count()).select_from(Booking).where(Booking.master_id == m))\n"
        "    await session.execute(pg_insert(MasterClient).values(master_id=m, user_id=2))\n"
        "    await session.execute(select(User).where(User.id == 1))\n"
    )
    assert sg.find_unscoped_queries(good, "x.py", models) == []


def test_query_whitelist_requires_explanations():
    for key, reason in {**sg.QUERY_WHITELIST, **sg.GET_WHITELIST, **sg.LRU_CACHE_WHITELIST}.items():
        assert len(reason) > 30, f"исключение {key} без внятного объяснения"


def test_constructor_guard_requires_master_id():
    models = sg.tenant_models()
    assert [f.line for f in sg.find_unscoped_constructors("s = Service(name='x')\nr = Review(master_id=1)", "x.py", models)] == [1]


# ---------- репозиторий без master_id ----------


def test_repository_construction_without_master_is_detected_statically():
    src = "a = BookingRepository(session)\nb = BookingRepository(session, 1)\nc = BookingRepository(session, master_id=2)"
    assert [f.line for f in sg.find_repositories_without_master(src, "x.py")] == [1]


def test_repository_classes_must_be_master_scoped():
    assert sg.find_repository_classes_not_scoped("class FooRepository:\n    pass", "x.py")
    assert not sg.find_repository_classes_not_scoped("class FooRepository(MasterScopedRepository):\n    pass", "x.py")


def _repository_classes() -> list[type]:
    classes = []
    for info in pkgutil.iter_modules(repositories_package.__path__):
        module = importlib.import_module(f"{repositories_package.__name__}.{info.name}")
        for obj in vars(module).values():
            if isinstance(obj, type) and obj.__name__.endswith("Repository") and obj is not MasterScopedRepository and obj.__module__ == module.__name__:
                classes.append(obj)
    return classes


@pytest.mark.parametrize("cls", _repository_classes(), ids=lambda c: c.__name__)
def test_every_repository_requires_master_id(cls):
    assert issubclass(cls, MasterScopedRepository)
    param = signature(cls.__init__).parameters["master_id"]
    assert param.default is Parameter.empty, "у master_id не должно быть значения по умолчанию"
    with pytest.raises(TypeError):
        cls(object())  # master_id не передан
    for bad in (None, 0, -1, True, "1", 1.5):
        with pytest.raises(MasterIdRequiredError):
            cls(object(), bad)
    assert cls(object(), 7).master_id == 7


def test_repository_count_is_what_we_expect():
    assert len(_repository_classes()) >= 6
