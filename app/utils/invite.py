"""Ссылка-приглашение клиента в бота: t.me/<бот>?start=inv_<токен>.

Мастер создаёт запись для человека, которого ещё нет в боте, и отправляет ему такую ссылку.
Когда человек открывает её, запись привязывается к нему (см. BookingService.claim_invitation).
"""
import secrets
from urllib.parse import quote

INVITE_START_PREFIX = "inv_"


def new_invite_token() -> str:
    # 9 байт -> 12 символов [A-Za-z0-9_-]: допустимо для параметра /start и не угадывается перебором.
    return secrets.token_urlsafe(9)


def build_invite_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start={INVITE_START_PREFIX}{token}"


def build_share_url(link: str, text: str) -> str:
    """Ссылка, открывающая в Telegram окно «Переслать» с готовым текстом: мастер сам выбирает чат
    с клиентом и отправляет сообщение со своего аккаунта."""
    return f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(text, safe='')}"