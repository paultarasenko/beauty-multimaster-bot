import datetime as dt
from decimal import Decimal

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.admin_keyboards import (
    BTN_STATS_3MONTHS,
    BTN_STATS_MONTH,
    BTN_STATS_REPORT,
    BTN_STATS_TODAY,
    BTN_STATS_WEEK,
    admin_stats_menu_keyboard,
    report_period_keyboard,
)
from app.bot.keyboards.callback_data import ReportCallback
from app.bot.middlewares.admin_filter import IsAdminFilter
from app.services.report_service import build_report_workbook
from app.services.stats_service import (
    PERIOD_3MONTHS,
    PERIOD_6MONTHS,
    PERIOD_MONTH,
    PERIOD_TODAY,
    PERIOD_WEEK,
    get_period_stats,
)
from app.utils.duration import format_duration
from app.utils.master_settings import DEFAULT_CURRENCY
from app.utils.timezone import format_date_ru, get_master_tz
from app.tenancy import TenantContext

router = Router(name="admin_stats")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

PERIOD_TITLES = {
    PERIOD_TODAY: "за сегодня",
    PERIOD_WEEK: "за неделю",
    PERIOD_MONTH: "за месяц",
    PERIOD_3MONTHS: "за три месяца",
    PERIOD_6MONTHS: "на 6 месяцев",
}


def _format_money(value: Decimal, currency: str = DEFAULT_CURRENCY) -> str:
    if value == value.to_integral_value():
        return f"{int(value)} {currency}"
    return f"{value:.2f} {currency}"


def _format_duration(total_minutes: int) -> str:
    # Общий формат («1 ч 25 мин»); для нулевых свободных часов в статистике оставляем «0 ч».
    return format_duration(total_minutes) if total_minutes else "0 ч"


def _format_period(start: dt.date, end: dt.date) -> str:
    if start == end:
        return format_date_ru(start)
    return f"{format_date_ru(start)} — {format_date_ru(end)}"


async def _answer_stats(message: Message, session: AsyncSession, period: str, tenant: TenantContext) -> None:
    tz = await get_master_tz(session, tenant.master_id)
    now = dt.datetime.now(tz)
    currency = (await tenant.get_settings()).currency
    stats, start_date, end_date = await get_period_stats(session, tenant.master_id, tz, period, now)

    text = (
        f"📊 Статистика {PERIOD_TITLES[period]} ({_format_period(start_date, end_date)})\n\n"
        f"📅 Записи: {stats.bookings_count}\n"
        f"💰 Фактический доход: {_format_money(stats.actual_revenue, currency)}\n"
        f"💰 Ожидаемый доход: {_format_money(stats.expected_revenue, currency)}\n"
        f"🕐 Свободные часы: {_format_duration(stats.free_minutes)}"
    )
    await message.answer(text)


@router.message(F.text == "📊 Статистика")
async def open_stats_menu(message: Message) -> None:
    await message.answer("📊 Выберите период для статистики или скачайте Excel-отчёт", reply_markup=admin_stats_menu_keyboard())


@router.message(F.text == BTN_STATS_TODAY)
async def show_today_stats(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    await _answer_stats(message, session, PERIOD_TODAY, tenant=tenant)


@router.message(F.text == BTN_STATS_WEEK)
async def show_week_stats(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    await _answer_stats(message, session, PERIOD_WEEK, tenant=tenant)


@router.message(F.text == BTN_STATS_MONTH)
async def show_month_stats(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    await _answer_stats(message, session, PERIOD_MONTH, tenant=tenant)


@router.message(F.text == BTN_STATS_3MONTHS)
async def show_3months_stats(message: Message, session: AsyncSession, tenant: TenantContext) -> None:
    await _answer_stats(message, session, PERIOD_3MONTHS, tenant=tenant)


@router.message(F.text == BTN_STATS_REPORT)
async def ask_report_period(message: Message) -> None:
    await message.answer("📄 За какой период выгрузить отчёт?", reply_markup=report_period_keyboard())


@router.callback_query(ReportCallback.filter())
async def send_report(callback: CallbackQuery, callback_data: ReportCallback, session: AsyncSession, tenant: TenantContext) -> None:
    if callback_data.period not in PERIOD_TITLES:  # подделанная кнопка: неизвестный период
        await callback.answer("Эта кнопка устарела. Откройте раздел заново.", show_alert=True)
        return
    tz = await get_master_tz(session, tenant.master_id)
    now = dt.datetime.now(tz)
    buffer, filename = await build_report_workbook(
        session, tenant.master_id, tz, callback_data.period, now, (await tenant.get_settings()).currency
    )
    await callback.message.answer_document(
        BufferedInputFile(buffer.read(), filename=filename),
        caption=f"📄 Отчёт {PERIOD_TITLES[callback_data.period]}",
    )
    await callback.answer()
