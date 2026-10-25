"""Кнопки периода Excel-отчёта: «На три месяца» и «На 6 месяцев» считают период вперёд от сегодняшнего дня."""

import datetime as dt

from app.bot.keyboards.admin_keyboards import report_period_keyboard
from app.bot.keyboards.callback_data import ReportCallback
from app.services.report_service import build_report_workbook
from app.services.stats_service import PERIOD_3MONTHS, PERIOD_6MONTHS
from tests.conftest import MASTER_ID, RIGA_TZ


def test_report_keyboard_has_forward_periods_and_a_long_six_month_button():
    rows = report_period_keyboard().inline_keyboard
    assert [[b.text for b in row] for row in rows] == [
        ["📌 За сегодня", "📅 За неделю"],
        ["🗓️ За месяц", "📈 На три месяца"],
        ["📊 На 6 месяцев"],  # длинная кнопка отдельной строкой
    ]
    assert rows[1][1].callback_data == ReportCallback(period=PERIOD_3MONTHS).pack()
    assert rows[2][0].callback_data == ReportCallback(period=PERIOD_6MONTHS).pack()


async def test_six_month_report_covers_180_days_from_today(session, master_profile):
    now = dt.datetime(2030, 1, 16, 12, 0, tzinfo=RIGA_TZ)
    _, filename = await build_report_workbook(session, MASTER_ID, RIGA_TZ, PERIOD_6MONTHS, now)
    assert filename == "otchet_16012030-14072030.xlsx"
