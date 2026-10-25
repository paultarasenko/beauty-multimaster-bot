"""Этап 11: самотесты усиленного сторожа (tests/static_guards.py) на заведомо плохих фрагментах.

Сторож, который «ничего не находит», бесполезен: здесь доказано, что он ловит сырой SQL и запрос к таблице мастера в новой
функции файла, исключённого из правила, — и не ругается на хороший код. Тесты не требуют БД.
"""

import ast

from tests import static_guards as sg

MODELS = sg.tenant_models()


def lines(findings) -> list[int]:
    return sorted(f.line for f in findings)


# ---------------------------------------------------------------- сырой SQL


def test_raw_sql_guard_detects_text_and_string_execute():
    bad = (
        "from sqlalchemy import text, select\n"  # 1
        "async def a(session):\n"  # 2
        "    await session.execute(text('SELECT * FROM bookings'))\n"  # 3 text()
        "async def b(session, x):\n"  # 4
        "    await session.execute(f'DELETE FROM services WHERE id = {x}')\n"  # 5 f-строка
        "async def c(session):\n"  # 6
        "    await session.execute('UPDATE reviews SET active = false')\n"  # 7 строка
        "async def d(conn):\n"  # 8
        "    return await conn.fetch('SELECT * FROM users')\n"  # 9 asyncpg
        "async def e(session, x):\n"  # 10
        "    await session.execute('SELECT ' + x)\n"  # 11 склейка
        "async def f(engine):\n"  # 12
        "    engine.exec_driver_sql('TRUNCATE bookings')\n"  # 13
        "async def g(session):\n"  # 14
        "    await session.execute(select(1))\n"  # 15 — нормально
    )
    assert lines(sg.find_raw_sql(bad, "app/x.py", whitelist={})) == [3, 5, 7, 9, 11, 13]


def test_raw_sql_guard_detects_aliased_import_and_sa_text():
    bad = (
        "import sqlalchemy as sa\n"
        "from sqlalchemy import text as raw\n"
        "def a(s):\n"
        "    s.execute(raw('select 1'))\n"  # 4
        "def b(s):\n"
        "    s.execute(sa.text('select 1'))\n"  # 6
    )
    assert lines(sg.find_raw_sql(bad, "app/x.py", whitelist={})) == [4, 6]


def test_raw_sql_guard_ignores_non_sqlalchemy_text_and_orm_queries():
    good = (
        "async def a(session, message, m):\n"
        "    text = message.text\n"
        "    await message.answer(text)\n"
        "    await session.execute(select(Booking).where(Booking.master_id == m))\n"
        "    await session.execute(update(Service).where(Service.master_id == m).values(active=False))\n"
    )
    assert sg.find_raw_sql(good, "app/x.py", whitelist={}) == []


def test_raw_sql_whitelist_is_per_function():
    src = "from sqlalchemy import text\nasync def _check_db(s):\n    await s.execute(text('SELECT 1'))\nasync def other(s):\n    await s.execute(text('SELECT 2'))\n"
    findings = sg.find_raw_sql(src, "app/multibot/webhook.py")
    assert [(f.line, f.rule) for f in findings] == [(5, "raw-sql")]  # _check_db разрешён, other — нет


def test_raw_sql_whitelist_has_reasons_and_points_to_real_raw_sql():
    """Исключения из правила 6 не должны устаревать: каждая запись указывает на функцию, где сырой SQL действительно есть."""
    for (path, function), reason in sg.RAW_SQL_WHITELIST.items():
        assert len(reason) > 30, f"исключение {(path, function)} без внятного объяснения"
        source = (sg.PROJECT_ROOT / path).read_text(encoding="utf-8")
        assert any(f.rule == "raw-sql" for f in sg.find_raw_sql(source, path, whitelist={})), f"{path}: сырого SQL нет — уберите запись"
        assert any(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function for n in ast.walk(ast.parse(source))
        ), f"{path}: функции {function} нет"


def test_app_has_no_unexplained_raw_sql_including_stage_9_and_10_code():
    scanned = {sg._rel(p) for p in sg.app_python_files()}
    for stage_9_10 in (
        "app/superadmin/service.py",
        "app/superadmin/handlers.py",
        "app/superadmin/digest.py",
        "app/superadmin/trial.py",
        "app/multibot/management.py",
        "app/bot/disabled_replies.py",
        "app/bot/handlers/admin/my_bot.py",
        "app/services/my_bot_service.py",
    ):
        assert stage_9_10 in scanned, f"{stage_9_10} не попадает под сторожей"
    findings = []
    for path in sg.app_python_files(include_models=False):
        findings += sg.find_raw_sql(path.read_text(encoding="utf-8"), sg._rel(path))
    assert not findings, "сырой SQL без белого списка:\n" + "\n".join(str(f) for f in findings)


# ---------------------------------------------------------------- запрос в НОВОЙ функции «разрешённого» файла


def test_narrowed_whitelist_catches_new_unscoped_function_in_superadmin_service():
    """Файл app/superadmin/service.py исключён из правила 1 только для get_summary: новая функция с таким запросом — нарушение."""
    bad = (
        "async def get_summary(session):\n"  # разрешена исторически
        "    return await session.execute(select(func.count()).select_from(Booking))\n"  # 2
        "async def leak_all_bookings(session):\n"  # новая — не разрешена
        "    return await session.execute(select(Booking).where(Booking.status == 'confirmed'))\n"  # 4
        "async def leak_clients(session):\n"
        "    return await session.execute(select(MasterClient))\n"  # 6
    )
    findings = sg.find_unscoped_queries(bad, "app/superadmin/service.py", MODELS)
    assert lines(findings) == [4, 6]
    assert all("leak_" in f.detail for f in findings)


def test_narrowed_whitelist_still_accepts_scoped_queries_in_new_functions():
    good = (
        "async def new_card(session, master_id):\n"
        "    return await session.execute(select(Booking).where(Booking.master_id == master_id))\n"
    )
    assert sg.find_unscoped_queries(good, "app/superadmin/service.py", MODELS) == []


def test_whitelist_without_narrowing_keeps_old_whole_file_behaviour():
    """Совместимость с этапом 5: явный `whitelist` без `narrowed` действует на файл целиком."""
    src = "async def anything(session):\n    await session.execute(select(Booking))\n"
    assert sg.find_unscoped_queries(src, "x.py", MODELS, whitelist={("x.py", "Booking"): "причина " * 10}) == []
    narrowed = {("x.py", "Booking"): frozenset({"other"})}
    assert lines(sg.find_unscoped_queries(src, "x.py", MODELS, whitelist={("x.py", "Booking"): "r" * 40}, narrowed=narrowed)) == [2]


def test_narrowed_whitelist_entries_refer_to_existing_functions_and_whitelisted_files():
    for (path, model), functions in sg.QUERY_WHITELIST_FUNCTIONS.items():
        assert (path, model) in sg.QUERY_WHITELIST, f"{(path, model)} сужен, но не входит в QUERY_WHITELIST"
        names = {
            n.name
            for n in ast.walk(ast.parse((sg.PROJECT_ROOT / path).read_text(encoding="utf-8")))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert functions <= names, f"{path}: нет функций {sorted(functions - names)}"


# ---------------------------------------------------------------- прочие правила работают на плохом коде этапов 9–10


def test_guard_flags_model_created_without_master_id_and_repository_without_master():
    assert lines(sg.find_unscoped_constructors("a = MasterClient(user_id=1)\nb = Booking(master_id=1)", "app/x.py", MODELS)) == [1]
    assert lines(sg.find_repositories_without_master("r = ServiceRepository(session)", "app/x.py")) == [1]


def test_module_state_guard_flags_shared_dict_and_cache():
    bad = "from functools import lru_cache\nBOTS_BY_MASTER = {}\nstate = {}\n@lru_cache\ndef f(): ...\n"
    findings = sg.find_module_state(bad, "app/x.py")
    assert {f.rule for f in findings} == {"module-state", "cache-decorator"}
