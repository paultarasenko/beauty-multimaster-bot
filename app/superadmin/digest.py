"""Ежедневная сводка суперадминистраторам о сроках пробного периода (этап 10).

Задача общего планировщика (`SchedulerService`, id `trial_digest_worker`): проверка идёт каждые DIGEST_CHECK_INTERVAL_SECONDS,
а сама сводка отправляется не раньше SUPERADMIN_DIGEST_HOUR по часам SUPERADMIN_TIMEZONE (если в этот час процесс был выключен —
уйдёт на следующей проверке в тот же день). Одно сообщение на каждого суперадмина со списком мастеров:

- срок заканчивается завтра — один раз за день до конца;
- срок истёк, решение не принято — каждый день, пока суперадмин не продлит срок, не снимет его или не отключит мастера.

Отключённые мастера и мастера без срока в сводку не входят. «Что уже сообщали» хранится в masters (trial_expiring_notified_for,
trial_expired_notified_on): после перезапуска ничего не дублируется. Отметка ставится ПОСЛЕ успешной отправки хотя бы одному
суперадмину (сбой Telegram — повтор на следующей проверке; сбой одного получателя другим не мешает). Один планировщик на сервис,
поэтому атомарного «захвата» нет: два процесса в одну и ту же минуту могли бы продублировать сводку (лучше дубль, чем потеря
единственного предупреждения «заканчивается завтра»).
"""

import datetime as dt
import logging
from collections.abc import Iterable

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config.settings import settings
from app.multibot.redact import redact
from app.superadmin import service, trial
from app.superadmin.service import DigestItem
from app.superadmin.ui import _btn

logger = logging.getLogger(__name__)

DIGEST_CHECK_INTERVAL_SECONDS = 600
MAX_DIGEST_BUTTONS = 10
SEND_TIMEOUT_SECONDS = 10.0


def _line(item: DigestItem, text: str) -> str:
    bot = f", @{item.bot_username}" if item.bot_username else ""
    return f"• мастер №{item.master_id}{bot} — {text}"


def digest_text(expiring: list[DigestItem], expired: list[DigestItem], today: dt.date) -> str:
    lines = ["⏰ Пробные периоды мастеров\n"]
    if expired:
        lines.append("Срок истёк — нужно решение (продлить, снять срок или отключить):")
        for item in expired:
            lines.append(_line(item, f"закончился {trial.days_word((today - item.ends_on).days)} назад ({item.ends_on:%d.%m})"))
        lines.append("")
    if expiring:
        lines.append("Срок заканчивается завтра:")
        lines.extend(_line(item, f"до {item.ends_on:%d.%m} включительно") for item in expiring)
        lines.append("")
    total = len(expired) + len(expiring)
    if total > MAX_DIGEST_BUTTONS:
        lines.append(f"Кнопки показаны для первых {MAX_DIGEST_BUTTONS}; остальные — по кнопке «Требуют решения».")
    lines.append("Ничего не отключается автоматически: бот каждого мастера работает, пока вы сами не нажмёте «Отключить».")
    return "\n".join(lines)


def digest_keyboard(expiring: list[DigestItem], expired: list[DigestItem]) -> InlineKeyboardMarkup:
    """Кнопки открытия карточек первых MAX_DIGEST_BUTTONS мастеров (сначала с истёкшим сроком) + «Требуют решения»."""
    shown = (expired + expiring)[:MAX_DIGEST_BUTTONS]
    rows = [[_btn(f"№{item.master_id}" + (f" · @{item.bot_username}" if item.bot_username else ""), "card", item.master_id)] for item in shown]
    rows.append([_btn("⚠️ Требуют решения", "need")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class TrialDigest:
    """Формирует и отправляет сводку. Состояния в памяти нет — всё в БД."""

    def __init__(self, session_factory: async_sessionmaker, bot: Bot, recipients: Iterable[int]):
        self._session_factory = session_factory
        self._bot = bot
        self._recipients = tuple(recipients)

    async def run_once(self, now: dt.datetime | None = None) -> bool:
        """Одна проверка. True — сводка отправлена хотя бы одному суперадмину. Исключений не выбрасывает."""
        try:
            return await self._run(now)
        except Exception:
            logger.exception("Сводка о пробных периодах: сбой, повторим на следующей проверке")
            return False

    async def _run(self, now: dt.datetime | None) -> bool:
        now = now or dt.datetime.now(dt.timezone.utc)
        local = now.astimezone(trial.superadmin_tz())
        if local.hour < settings.superadmin_digest_hour:
            return False  # время сводки сегодня ещё не наступило
        today = local.date()
        expiring, expired = await service.list_digest_candidates(self._session_factory, today)
        if not expiring and not expired:
            return False

        text = digest_text(expiring, expired, today)
        markup = digest_keyboard(expiring, expired)
        delivered = 0
        for chat_id in self._recipients:
            try:
                await self._bot.send_message(chat_id, redact(text), reply_markup=markup, request_timeout=SEND_TIMEOUT_SECONDS)
                delivered += 1
            except Exception as exc:
                # Например, суперадмин ещё не нажимал /start у служебного бота (Forbidden) или нет связи с Telegram.
                logger.warning("Не удалось отправить сводку о сроках суперадмину %s: %s", chat_id, type(exc).__name__)
        if not delivered:
            return False  # никому не доставлено — отметок нет, повторим на следующей проверке
        await service.mark_digest_sent(
            self._session_factory, today, [i.master_id for i in expiring], [i.master_id for i in expired]
        )
        logger.info("Сводка о сроках отправлена (%s получ.): истёк — %s, заканчивается завтра — %s", delivered, len(expired), len(expiring))
        return True
