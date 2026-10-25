from app.utils.reminder_texts import (
    client_reminder_2h_text,
    client_reminder_24h_text,
    master_name_dative,
    master_reminder_2h_text,
)


def test_master_name_dative_common_names():
    assert master_name_dative("Анна Иванова") == "Анне"
    assert master_name_dative("Юля") == "Юле"
    assert master_name_dative("Мария Петрова") == "Марии"


def test_master_name_dative_unknown_ending_falls_back_to_neutral_form():
    assert master_name_dative("Любовь") == "мастеру Любовь"
    assert master_name_dative("") == "мастеру"


def test_client_reminder_2h_text():
    text = client_reminder_2h_text("Анна Иванова", "Маникюр", "15:00 - 16:30", 50, "г. Минск, ул. Примерная, 10")
    assert text == (
        "🔔 Через 2 часа у вас запись\n\n"
        "Маникюр\n"
        "🕐 15:00 - 16:30\n"
        "💰 50 BYN\n"
        "📍 г. Минск, ул. Примерная, 10"
    )


def test_client_reminder_2h_text_without_address():
    text = client_reminder_2h_text("Анна", "Маникюр", "15:00 - 16:00", 40.5)
    assert text == "🔔 Через 2 часа у вас запись\n\nМаникюр\n🕐 15:00 - 16:00\n💰 40.50 BYN"


def test_client_reminder_2h_text_escapes_user_input():
    text = client_reminder_2h_text("Анна", "Брови & <b>ресницы</b>", "15:00 - 16:00", 40, "ул. <Ленина> & 5")
    assert "Брови &amp; &lt;b&gt;ресницы&lt;/b&gt;" in text and "ул. &lt;Ленина&gt; &amp; 5" in text


def test_master_reminder_2h_text():
    text = master_reminder_2h_text("Анна", "anna_client", "💎 Инкрустация стразами", "08:00", 80, "120.00")
    assert text == (
        "🔔 Через 2 часа — запись на 08:00\n"
        "💎 Инкрустация стразами\n"
        "👤 Анна (@anna_client)\n"
        "⏱ 1 ч 20 мин · 💰 120 BYN"
    )


def test_master_reminder_2h_text_keeps_fractional_price():
    text = master_reminder_2h_text("Анна", "anna_client", "Маникюр", "15:00", 90, 40.5)
    assert "💰 40.50 BYN" in text


def test_master_reminder_2h_text_without_username():
    text = master_reminder_2h_text("Анна", None, "SPA-уход", "10:30", 60, 25)
    assert "👤 Анна (нет username)" in text


def test_master_reminder_2h_text_without_username_and_name():
    text = master_reminder_2h_text(None, None, "SPA-уход", "10:30", 60, 25)
    assert "👤 нет username" in text


def test_client_reminder_24h_text_shows_price_right_after_time():
    text = client_reminder_24h_text("Маникюр", "15:00 - 16:30", "50.00", "г. Минск, ул. Примерная, 10")
    assert text == (
        "👋 Напоминание о записи\n\n"
        "Завтра у вас запись:\n\n"
        "Маникюр\n"
        "🕐 15:00 - 16:30\n"
        "💰 50 BYN\n"
        "г. Минск, ул. Примерная, 10\n\n"
        "Пожалуйста, подтвердите, что всё в силе."
    )


def test_client_reminder_24h_text_keeps_fractional_price():
    assert "💰 40.50 BYN" in client_reminder_24h_text("Маникюр", "15:00 - 16:00", 40.5)


def test_client_reminder_24h_text_without_address():
    text = client_reminder_24h_text("Маникюр", "15:00 - 16:00", 40, None)
    assert text == (
        "👋 Напоминание о записи\n\n"
        "Завтра у вас запись:\n\n"
        "Маникюр\n"
        "🕐 15:00 - 16:00\n"
        "💰 40 BYN\n\n"
        "Пожалуйста, подтвердите, что всё в силе."
    )


def test_client_reminder_24h_text_escapes_user_input():
    text = client_reminder_24h_text("Брови & <b>ресницы</b>", "15:00 - 16:00", 40, "ул. <Ленина> & 5")
    assert "Брови &amp; &lt;b&gt;ресницы&lt;/b&gt;" in text and "ул. &lt;Ленина&gt; &amp; 5" in text
