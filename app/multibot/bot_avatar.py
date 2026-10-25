"""Аватарка бота в Telegram (setMyProfilePhoto / removeMyProfilePhoto): общий код для мастера («Фото профиля» в настройках)
и для применения эталонной базы суперадмином."""

import json

import aiohttp
from aiogram import Bot


class BotPhotoError(Exception):
    """Telegram не принял запрос на смену или удаление фото бота (текст можно показать мастеру)."""


async def call_bot_api(bot: Bot, method: str, photo: bytes | None = None) -> None:
    """Вызывает setMyProfilePhoto / removeMyProfilePhoto напрямую: в aiogram 3.15 этих методов ещё нет."""
    url = bot.session.api.api_url(token=bot.token, method=method)
    form = None
    if photo is not None:
        form = aiohttp.FormData()
        form.add_field("photo", json.dumps({"type": "static", "photo": "attach://avatar"}))
        form.add_field("avatar", photo, filename="avatar.jpg", content_type="image/jpeg")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
            async with http.post(url, data=form) as response:
                result = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise BotPhotoError("Не удалось связаться с Telegram. Попробуйте ещё раз чуть позже.") from exc
    if not result.get("ok"):
        raise BotPhotoError(result.get("description") or "Telegram не принял запрос.")


async def set_bot_avatar(bot: Bot, photo: bytes) -> None:
    await call_bot_api(bot, "setMyProfilePhoto", photo=photo)


async def remove_bot_avatar(bot: Bot) -> None:
    await call_bot_api(bot, "removeMyProfilePhoto")
