from aiogram.fsm.state import State, StatesGroup


class BookingFSM(StatesGroup):
    select_service = State()
    select_date = State()
    select_time = State()
    confirm = State()


class CheckTimeFSM(StatesGroup):
    select_date = State()


class RescheduleFSM(StatesGroup):
    select_date = State()
    select_time = State()
    confirm = State()


class AdminAddBookingFSM(StatesGroup):
    select_service = State()
    select_date = State()
    select_time = State()
    enter_client_name = State()
    enter_client_contact = State()
    confirm = State()


class AdminScheduleFSM(StatesGroup):
    select_start_time = State()
    select_end_time = State()
    confirm = State()
    confirm_day_off = State()
    lunch_menu = State()
    lunch_start = State()
    lunch_end = State()
    lunch_confirm = State()


class AdminSpecialDayFSM(StatesGroup):
    select_date = State()
    day_menu = State()
    select_start_time = State()
    select_end_time = State()
    confirm = State()
    confirm_close = State()


class AdminBlockTimeFSM(StatesGroup):
    select_date = State()
    select_start_time = State()
    select_end_time = State()
    confirm = State()


class AdminAboutFSM(StatesGroup):
    edit_field = State()
    edit_name = State()


class AdminServiceFSM(StatesGroup):
    enter_name = State()
    enter_price = State()
    enter_duration = State()
    enter_description = State()
    edit_name = State()
    edit_price = State()
    edit_description = State()
    edit_duration = State()


class ReviewFSM(StatesGroup):
    enter_name = State()
    enter_text = State()


class AdminPortfolioFSM(StatesGroup):
    add_photo = State()
    add_title = State()
    add_description = State()
    edit_title = State()
    edit_description = State()


class AdminReminderFSM(StatesGroup):
    enter_date = State()
    enter_time = State()
    enter_text = State()


class AdminSettingsFSM(StatesGroup):
    """Ввод в «⚙️ Настройки»: валюта, индивидуальные тексты и фото профиля."""

    enter_currency = State()
    enter_text = State()
    enter_photo = State()


class MasterRequestFSM(StatesGroup):
    """Клиент пишет мастеру, когда у него максимум активных записей."""

    waiting_message = State()


class MasterReplyFSM(StatesGroup):
    """Мастер пишет клиенту сообщение к своему решению по запросу."""

    waiting_text = State()


class ContactMasterFSM(StatesGroup):
    """Ограниченный клиент пишет мастеру через «📞 Связаться со мной»."""

    waiting_message = State()
