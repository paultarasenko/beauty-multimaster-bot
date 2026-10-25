"""Управление мастерами и ботами: python -m app.cli <команда>.

  create-master  --owner-telegram-id ID [--trial-days N]   (без --trial-days — без срока пробного периода)
  add-bot        --master-id N --token TOKEN     (токен можно передать через переменную BOT_TOKEN_TO_ADD)
  remove-bot     --master-id N
  refresh-webhook --master-id N [--rotate-secret]
  list-bots
"""

import argparse
import asyncio
import logging
import os
import sys

from app.config.settings import settings
from app.database.session import async_session_factory
from app.multibot import management
from app.multibot.crypto import ConfigurationError, TokenCipher, TokenDecryptionError
from app.multibot.redact import install_log_redaction
from app.superadmin.trial import MAX_TRIAL_DAYS, new_master_ends_on, superadmin_today


def _trial_days(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not 1 <= value <= MAX_TRIAL_DAYS:
        raise argparse.ArgumentTypeError(f"нужно целое число дней от 1 до {MAX_TRIAL_DAYS}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Управление мастерами и их Telegram-ботами")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-master", help="создать мастера")
    p.add_argument("--owner-telegram-id", type=int, required=True)
    p.add_argument(
        "--trial-days",
        type=_trial_days,
        default=None,
        help=f"срок пробного периода в днях (1–{MAX_TRIAL_DAYS}) от сегодняшней даты; не указан — без срока "
        "(TRIAL_DEFAULT_DAYS из .env здесь не применяется)",
    )

    p = sub.add_parser("add-bot", help="подключить бота мастеру и установить webhook")
    p.add_argument("--master-id", type=int, required=True)
    p.add_argument("--token", help="токен бота; если не указан — переменная окружения BOT_TOKEN_TO_ADD")

    p = sub.add_parser("remove-bot", help="отключить бота мастера (webhook снимается)")
    p.add_argument("--master-id", type=int, required=True)

    p = sub.add_parser("refresh-webhook", help="повторно установить webhook бота мастера")
    p.add_argument("--master-id", type=int, required=True)
    p.add_argument("--rotate-secret", action="store_true", help="выдать новый секрет webhook")

    sub.add_parser("list-bots", help="показать мастеров и ботов (без токенов)")
    return parser


async def run(args: argparse.Namespace, *, session_factory=None, bot_factory=None, fsm_cleaner=None, out=print) -> int:
    session_factory = session_factory or async_session_factory
    bot_factory = bot_factory or management.default_bot_factory
    fsm_cleaner = fsm_cleaner or management.default_fsm_cleaner
    base_url = settings.public_base_url
    if args.command == "create-master":
        ends_on = new_master_ends_on(getattr(args, "trial_days", None), superadmin_today())
        master_id = await management.create_master(session_factory, args.owner_telegram_id, trial_ends_on=ends_on)
        term = f"пробный период до {ends_on:%d.%m.%Y} включительно" if ends_on else "без срока"
        out(f"Мастер создан: id={master_id}, владелец={args.owner_telegram_id}, {term}")
        return 0

    if args.command == "list-bots":
        rows = await management.list_bots(session_factory)
        if not rows:
            out("Мастеров нет")
        for r in rows:
            bot = f"бот @{r.bot_username} (id={r.telegram_bot_id}, {'активен' if r.bot_active else 'выключен'})" if r.telegram_bot_id else "бота нет"
            out(f"master_id={r.master_id} владелец={r.owner_telegram_id} {'активен' if r.master_active else 'ОТКЛЮЧЁН'}: {bot}")
        return 0

    cipher = TokenCipher.from_settings()
    if args.command == "add-bot":
        token = args.token or os.environ.get("BOT_TOKEN_TO_ADD", "")
        if not token:
            raise management.ManagementError("Укажите --token или переменную BOT_TOKEN_TO_ADD")
        row = await management.add_bot(
            session_factory,
            cipher,
            master_id=args.master_id,
            token=token,
            public_base_url=base_url,
            bot_factory=bot_factory,
            fsm_cleaner=fsm_cleaner,
        )
        out(f"Бот @{row.bot_username} (id={row.telegram_bot_id}) подключён к мастеру {args.master_id}; webhook установлен")
    elif args.command == "remove-bot":
        warning = await management.remove_bot(
            session_factory, cipher, master_id=args.master_id, bot_factory=bot_factory, fsm_cleaner=fsm_cleaner
        )
        out(f"Бот мастера {args.master_id} отключён")
        if warning:
            out(f"Предупреждение: {warning}")
    elif args.command == "refresh-webhook":
        row = await management.refresh_webhook(
            session_factory,
            cipher,
            master_id=args.master_id,
            public_base_url=base_url,
            rotate_secret=args.rotate_secret,
            bot_factory=bot_factory,
        )
        out(f"Webhook бота @{row.bot_username} (id={row.telegram_bot_id}) обновлён")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    install_log_redaction()
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (management.ManagementError, ConfigurationError, TokenDecryptionError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
