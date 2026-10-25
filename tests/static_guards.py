"""Статический анализ (AST) для тестов-сторожей этапа 5. Только стандартная библиотека: модуль можно запускать и без БД.

Что ищем:
1. Запрос к таблице данных мастера без фильтра по master_id (`select/update/delete/insert` и `session.get`).
2. Создание модели данных мастера без `master_id=`.
3. Создание репозитория без master_id.
4. Репозитории, не наследующие MasterScopedRepository.
5. Состояние уровня модуля, которое могло бы быть «общим на процесс» (изменяемые контейнеры, `global`).
6. (этап 11) Сырой SQL: `text(...)`, `exec_driver_sql`, `execute("...")`/`fetch("...")` со строкой — такой запрос обходит
   проверку «фильтр по master_id» из правила 1, поэтому допускается только по явному белому списку (RAW_SQL_WHITELIST).

Таблицы данных мастера определяются автоматически: это классы в app/database/models, у которых есть поле `master_id`.
Новая модель с `master_id` попадает под правила без правки сторожа.
"""

import ast
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_ROOT / "app"
MODELS_DIR = APP_DIR / "database" / "models"
REPOSITORIES_DIR = APP_DIR / "database" / "repositories"

STATEMENT_BUILDERS = {"select", "update", "delete", "insert", "pg_insert"}

# Явные исключения из правила «запрос обязан фильтровать по master_id»: (путь от корня проекта, модель) -> почему.
# Любое новое исключение — осознанное решение, а не способ заставить тест замолчать.
QUERY_WHITELIST: dict[tuple[str, str], str] = {
    ("app/multibot/registry.py", "MasterBot"): (
        "Реестр ботов ОПРЕДЕЛЯЕТ мастера по боту, принявшему апдейт (bot_id -> master_id), поэтому ищет строку "
        "master_bots по telegram_bot_id/webhook, а не внутри одного мастера. Данные мастера он не читает."
    ),
    ("app/multibot/management.py", "MasterBot"): (
        "CLI-управление ботами (create-master/add-bot/remove-bot/list-bots) — операции оператора платформы поверх "
        "всех мастеров (уникальность telegram_bot_id, список ботов). Не пользовательский слой и не данные клиентов."
    ),
    ("app/superadmin/service.py", "MasterBot"): (
        "Суперадмин (этап 9) — владелец сервиса управляет ВСЕМИ мастерами: список мастеров с ботами, сводка, включение/выключение "
        "бота, обход активных ботов для проверки webhook. Этап 10: выбор подключённых мастеров со сроком пробного периода для "
        "ежедневной сводки (list_digest_candidates — LEFT JOIN master_bots ради @username бота). Явная сервисная функция вместо "
        "репозитория мастера; токены и секреты наружу не отдаёт."
    ),
    ("app/superadmin/service.py", "MasterClient"): (
        "Суперадмин (этап 9): сводка и карточка мастера показывают ТОЛЬКО число клиентов (count по всем мастерам) — "
        "без персональных данных."
    ),
    ("app/superadmin/service.py", "Booking"): (
        "Суперадмин (этап 9): сводка и карточка мастера показывают ТОЛЬКО число записей (count по всем мастерам) — "
        "без персональных данных."
    ),
}

# Этап 11: белый список QUERY_WHITELIST действует на файл целиком, а значит новая функция в том же файле молча получала
# право читать таблицу без фильтра. Для файлов ниже исключение сужено до перечисленных функций: новая функция с таким же
# запросом (например, в app/superadmin/service.py этапов 9–10) снова считается нарушением, пока её осознанно не добавят сюда.
# (путь, модель) -> имена функций. Файлов без записи здесь это не касается (исключение — на весь файл, как раньше).
QUERY_WHITELIST_FUNCTIONS: dict[tuple[str, str], frozenset[str]] = {
    ("app/superadmin/service.py", "MasterBot"): frozenset({"get_summary"}),
    ("app/superadmin/service.py", "MasterClient"): frozenset({"get_summary"}),
    ("app/superadmin/service.py", "Booking"): frozenset({"get_summary"}),
    ("app/multibot/registry.py", "MasterBot"): frozenset({"deactivate_bot"}),
    ("app/multibot/management.py", "MasterBot"): frozenset({"add_bot"}),
}

# Сырой SQL (правило 6): (путь, функция) -> почему допустим. Сырой запрос не должен трогать данные мастеров.
RAW_SQL_WHITELIST: dict[tuple[str, str], str] = {
    ("app/multibot/webhook.py", "_check_db"): (
        "Проверка готовности /ready: константный `SELECT 1` без таблиц — к данным мастеров не обращается."
    ),
    ("app/scheduler/locks.py", "tick_lock"): (
        "Этап 12C: `pg_try_advisory_lock` / `pg_advisory_unlock` — служебные функции PostgreSQL без таблиц; замок общий на "
        "тик (все мастера сразу) и данных мастеров не читает и не меняет."
    ),
}

# Исключения для `session.get(Model, id)` по таблицам данных мастера (кроме репозиториев): (путь, модель) -> почему.
GET_WHITELIST: dict[tuple[str, str], str] = {}

# Модули, где разрешено изменяемое состояние уровня модуля (ничего такого сейчас нет): путь -> почему.
MODULE_STATE_WHITELIST: dict[str, str] = {}
# lru_cache допустим только для вычислений, не зависящих от мастера: (путь, функция) -> почему.
LRU_CACHE_WHITELIST: dict[tuple[str, str], str] = {
    ("app/bot/middlewares/reset_input.py", "menu_button_texts"): (
        "Тексты кнопок меню берутся из клавиатур без данных: одинаковы для всех мастеров и процессов, пересчитать их "
        "можно в любой момент — кэш лишь экономит построение клавиатур."
    ),
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.detail}"


def tenant_models() -> set[str]:
    """Имена моделей, у которых есть колонка master_id (таблицы данных мастера)."""
    names: set[str] = set()
    for path in sorted(MODELS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                target = item.target if isinstance(item, ast.AnnAssign) else None
                if isinstance(target, ast.Name) and target.id == "master_id":
                    names.add(node.name)
    return names


def _rel(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _chain_root(node: ast.Call, parents: dict[ast.AST, ast.AST]) -> ast.AST:
    """Вся цепочка `select(...).where(...).order_by(...)`: поднимаемся, пока вызов — часть цепочки методов."""
    current: ast.AST = node
    while True:
        parent = parents.get(current)
        grand = parents.get(parent) if parent is not None else None
        if isinstance(parent, ast.Attribute) and parent.value is current and isinstance(grand, ast.Call) and grand.func is parent:
            current = grand
            continue
        return current


def _names_and_scoped(expr: ast.AST, models: set[str]) -> tuple[set[str], set[str]]:
    """(модели данных мастера, упомянутые в выражении; модели, у которых есть `Model.master_id` или kw `master_id=`)."""
    mentioned: set[str] = set()
    scoped: set[str] = set()
    has_master_keyword = False
    for node in ast.walk(expr):
        if isinstance(node, ast.Name) and node.id in models:
            mentioned.add(node.id)
        if isinstance(node, ast.Attribute) and node.attr == "master_id" and isinstance(node.value, ast.Name):
            scoped.add(node.value.id)
        if isinstance(node, ast.keyword) and node.arg == "master_id":
            has_master_keyword = True
    if has_master_keyword:  # insert(Model).values(master_id=...) — вставка проставляет мастера явно
        scoped |= mentioned
    return mentioned, scoped


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    """Имя ближайшей объемлющей функции ("<module>", если запрос на уровне модуля)."""
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(current)
    return "<module>"


def _whitelisted_here(path: str, model: str, function: str, whitelist: dict, narrowed: dict) -> bool:
    """Файл в белом списке И (если для него задано сужение) функция входит в разрешённые."""
    if (path, model) not in whitelist:
        return False
    allowed = narrowed.get((path, model))
    return allowed is None or function in allowed


def find_unscoped_queries(
    source: str, path: str, models: set[str], whitelist: dict | None = None, narrowed: dict | None = None
) -> list[Finding]:
    """Правило 1: запросы и `session.get` к таблицам мастера без фильтра по master_id.

    Этап 11: исключение из QUERY_WHITELIST можно сузить до функций (`narrowed`, по умолчанию QUERY_WHITELIST_FUNCTIONS):
    новая функция в «разрешённом» файле с таким же запросом снова считается нарушением. Если тест передаёт собственный
    `whitelist` без `narrowed`, сужения нет — исключение действует на файл целиком, как раньше.
    """
    if narrowed is None:
        narrowed = QUERY_WHITELIST_FUNCTIONS if whitelist is None else {}
    whitelist = QUERY_WHITELIST if whitelist is None else whitelist
    tree = ast.parse(source)
    parents = _parents(tree)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in STATEMENT_BUILDERS:
            root = _chain_root(node, parents)
            mentioned, scoped = _names_and_scoped(root, models)
            for model in sorted(mentioned - scoped):
                function = _enclosing_function(node, parents)
                if _whitelisted_here(path, model, function, whitelist, narrowed):
                    continue
                where = f" (функция {function})" if (path, model) in whitelist else ""
                findings.append(Finding(path, node.lineno, "query-without-master-id", f"{func.id}(...) к {model} без {model}.master_id{where}"))
        elif isinstance(func, ast.Attribute) and func.attr == "get" and node.args and isinstance(node.args[0], ast.Name):
            model = node.args[0].id
            if model in models and (path, model) not in GET_WHITELIST:
                findings.append(Finding(path, node.lineno, "get-without-master-id", f".get({model}, id) читает строку только по id"))
    return findings


def _sqlalchemy_text_names(tree: ast.AST) -> set[str]:
    """Локальные имена, под которыми импортирован sqlalchemy.text (`from sqlalchemy import text [as t]`)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "sqlalchemy":
            for alias in node.names:
                if alias.name == "text":
                    names.add(alias.asname or alias.name)
    return names


def _is_string_expression(node: ast.AST) -> bool:
    """Строковый литерал, f-строка или склейка/форматирование строки (`"..." + x`, `"..." % x`)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _is_string_expression(node.left) or _is_string_expression(node.right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return _is_string_expression(node.func.value)
    return False


RAW_EXECUTE_METHODS = {"execute", "executemany", "fetch", "fetchrow", "fetchval", "fetchmany"}


def find_raw_sql(source: str, path: str, whitelist: dict | None = None) -> list[Finding]:
    """Правило 6 (этап 11): сырой SQL — `text(...)`, `sa.text(...)`, `exec_driver_sql`, `execute("строка")` и т.п.

    Сырой запрос невидим для правила 1 (оно ищет модели в `select/update/delete`), поэтому мог бы читать или менять данные
    всех мастеров. Разрешается только по RAW_SQL_WHITELIST: (путь, функция) -> причина. Модели (`app/database/models`)
    сторож не проверяет: там `text()` — фрагменты DDL (server_default, postgresql_where).
    """
    whitelist = RAW_SQL_WHITELIST if whitelist is None else whitelist
    tree = ast.parse(source)
    parents = _parents(tree)
    text_names = _sqlalchemy_text_names(tree)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        reason = None
        if isinstance(func, ast.Name) and func.id in text_names:
            reason = "text(...)"
        elif isinstance(func, ast.Attribute) and func.attr == "text" and isinstance(func.value, ast.Name) and func.value.id in {"sa", "sqlalchemy"}:
            reason = f"{func.value.id}.text(...)"
        elif isinstance(func, ast.Attribute) and func.attr == "exec_driver_sql":
            reason = "exec_driver_sql(...)"
        elif isinstance(func, ast.Attribute) and func.attr in RAW_EXECUTE_METHODS and node.args and _is_string_expression(node.args[0]):
            reason = f".{func.attr}(<строка SQL>)"
        if reason is None:
            continue
        function = _enclosing_function(node, parents)
        if (path, function) in whitelist:
            continue
        findings.append(Finding(path, node.lineno, "raw-sql", f"{reason} в функции {function}: сырой SQL обходит проверку master_id"))
    return findings


def find_unscoped_constructors(source: str, path: str, models: set[str]) -> list[Finding]:
    """Правило 2: `Booking(...)` и т.п. без `master_id=` (запись попала бы не туда или упала бы поздно, на NOT NULL)."""
    findings = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in models:
            if not any(kw.arg == "master_id" for kw in node.keywords):
                findings.append(Finding(path, node.lineno, "model-without-master-id", f"{node.func.id}(...) создаётся без master_id="))
    return findings


def find_repositories_without_master(source: str, path: str) -> list[Finding]:
    """Правило 3: `XRepository(session)` — репозиторий без мастера (нужен второй аргумент или `master_id=`)."""
    findings = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if name.endswith("Repository") and name != "MasterScopedRepository":
            has_master = len(node.args) >= 2 or any(kw.arg == "master_id" for kw in node.keywords)
            has_star = any(isinstance(a, ast.Starred) for a in node.args) or any(kw.arg is None for kw in node.keywords)
            if not has_master and not has_star:
                findings.append(Finding(path, node.lineno, "repository-without-master-id", f"{name}(...) создаётся без master_id"))
    return findings


def find_repository_classes_not_scoped(source: str, path: str) -> list[Finding]:
    """Правило 4: каждый класс `*Repository` обязан наследоваться от MasterScopedRepository."""
    findings = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name.endswith("Repository") and node.name != "MasterScopedRepository":
            bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
            if "MasterScopedRepository" not in bases:
                findings.append(Finding(path, node.lineno, "repository-not-scoped", f"{node.name} не наследует MasterScopedRepository"))
    return findings


def find_module_state(source: str, path: str) -> list[Finding]:
    """Правило 5: изменяемое состояние уровня модуля и `global`.

    Состояние в памяти процесса — не место для данных мастера: процессов может быть несколько (этап 7), а мастеров — сто.
    Константы (ИМЕНА_ЗАГЛАВНЫМИ) разрешены: их не меняют. Всё остальное — отдельным осознанным исключением.
    """
    findings: list[Finding] = []
    tree = ast.parse(source)
    mutable_calls = {"dict", "list", "set", "defaultdict", "OrderedDict", "Counter", "deque", "WeakValueDictionary"}
    if path not in MODULE_STATE_WHITELIST:
        for node in tree.body:
            targets, value = [], None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            if value is None:
                continue
            is_mutable = isinstance(value, (ast.Dict, ast.List, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)) or (
                isinstance(value, ast.Call) and isinstance(value.func, (ast.Name, ast.Attribute))
                and (value.func.id if isinstance(value.func, ast.Name) else value.func.attr) in mutable_calls
            )
            for target in targets:
                if is_mutable and isinstance(target, ast.Name) and not target.id.isupper() and target.id != "__all__":
                    findings.append(Finding(path, node.lineno, "module-state", f"изменяемое состояние уровня модуля «{target.id}»"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            findings.append(Finding(path, node.lineno, "global-statement", "global: состояние процесса"))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                name = target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else ""
                if name in {"lru_cache", "cache", "cached_property"} and (path, node.name) not in LRU_CACHE_WHITELIST:
                    findings.append(Finding(path, node.lineno, "cache-decorator", f"@{name} на {node.name}: кэш в памяти процесса"))
    return findings


def app_python_files(*, include_repositories: bool = True, include_models: bool = True) -> list[Path]:
    files = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if not include_repositories and REPOSITORIES_DIR in path.parents:
            continue
        if not include_models and MODELS_DIR in path.parents:
            continue
        files.append(path)
    return files


def scan_app() -> list[Finding]:
    """Все правила по всему app/. Запросы — везде, кроме репозиториев и моделей (там фильтр по master_id — сама суть)."""
    models = tenant_models()
    findings: list[Finding] = []
    for path in app_python_files():
        source = path.read_text(encoding="utf-8")
        rel = _rel(path)
        in_repo, in_models = REPOSITORIES_DIR in path.parents, MODELS_DIR in path.parents
        if not in_repo and not in_models:
            findings += find_unscoped_queries(source, rel, models)
        if not in_models:
            findings += find_unscoped_constructors(source, rel, models)
            findings += find_repositories_without_master(source, rel)
        if in_repo:
            findings += find_repository_classes_not_scoped(source, rel)
        if not in_models:
            findings += find_raw_sql(source, rel)
        findings += find_module_state(source, rel)
    return findings


if __name__ == "__main__":  # python -m tests.static_guards — быстрый ручной прогон
    print("модели данных мастера:", ", ".join(sorted(tenant_models())))
    result = scan_app()
    for finding in result:
        print(finding)
    print(f"нарушений: {len(result)}")
