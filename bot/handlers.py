"""Alert rendering, inline status buttons, reply menu and the note FSM."""

from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from core.crud import add_fast_track_account, get_all_smart_accounts, remove_fast_track_account
from core.database import async_session
from core.models import FeedEvent, Project, ProjectStatus
from services.digest import _chunks, send_digest

router = Router(name="alerts")

BTN_INBOX = "📥 Inbox"
BTN_HOLDING = "💎 Holding"
BTN_PRIORITY = "🔥 Priority"
BTN_SMARTS = "👁 Smarts"
BTN_DIGEST = "🗞 Дайджест"

START_TEXT = (
    "Web3 Alpha Radar онлайн.\n"
    "Кнопки внизу открывают списки.\n"
    "/add @user — Fast-Track (новые твиты сразу)\n"
    "/remove @user — убрать из Fast-Track\n"
    "/list — кто на отслеживании"
)

STATUS_CALLBACKS: dict[str, ProjectStatus] = {
    "status_research": ProjectStatus.RESEARCH,
    "status_wl": ProjectStatus.WL_HUNT,
    "status_holding": ProjectStatus.HOLDING,
    "status_inbox": ProjectStatus.INBOX,
    "status_blacklist": ProjectStatus.BLACKLIST,
}

STATUS_LABELS: dict[ProjectStatus, str] = {
    ProjectStatus.INBOX: "📥 Inbox",
    ProjectStatus.HIGH_PRIORITY: "🔥 High priority",
    ProjectStatus.RESEARCH: "🔍 Research",
    ProjectStatus.WL_HUNT: "🎯 WL-Hunt",
    ProjectStatus.HOLDING: "💎 Holding",
    ProjectStatus.ARCHIVED: "🗄 Archived",
    ProjectStatus.BLACKLIST: "⛔ Blacklist",
}


class NoteStates(StatesGroup):
    waiting_for_text = State()


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_INBOX), KeyboardButton(text=BTN_HOLDING)],
            [KeyboardButton(text=BTN_PRIORITY), KeyboardButton(text=BTN_SMARTS)],
            [KeyboardButton(text=BTN_DIGEST)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def build_alert_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔍 Research", callback_data=f"status_research:{project_id}"),
                InlineKeyboardButton(text="🎯 WL-Hunt", callback_data=f"status_wl:{project_id}"),
            ],
            [
                InlineKeyboardButton(text="💎 Holding", callback_data=f"status_holding:{project_id}"),
                InlineKeyboardButton(text="📥 Inbox", callback_data=f"status_inbox:{project_id}"),
            ],
            [
                InlineKeyboardButton(text="📝 Заметка", callback_data=f"note:{project_id}"),
                InlineKeyboardButton(text="⛔ Бан", callback_data=f"status_blacklist:{project_id}"),
            ],
        ]
    )


def build_alert_text(project: Project, event: FeedEvent | None = None) -> str:
    handle = escape(project.handle)
    lines = [
        "🚨🚨🚨 <b>ГРОМКИЙ АЛЕРТ</b> 🚨🚨🚨",
        "",
        f'<b>Проект:</b> <a href="https://x.com/{handle}">@{handle}</a>',
    ]
    if project.project_type:
        lines.append(f"<b>Тип:</b> {project.project_type.value}")
    if project.chain:
        lines.append(f"<b>Сеть:</b> {escape(project.chain)}")
    if project.contract_address:
        lines.append(f"<b>Контракт:</b> <code>{escape(project.contract_address)}</code>")
    lines.append(f"<b>Score:</b> {project.score or 0:.1f}")
    if project.is_tier1_backed:
        lines.append("💰 <b>Tier-1 backed</b>")
    if project.source:
        lines.append(f"<b>Источник:</b> {project.source.value}")
    status = project.status or ProjectStatus.INBOX
    lines.append(f"<b>Статус:</b> {STATUS_LABELS.get(status, status.value)}")

    if event is not None:
        lines.append("")
        who = f"@{escape(event.smart_account.handle)}" if event.smart_account else "—"
        lines.append(f"<b>Событие:</b> {event.event_type.value} от {who}")
        if event.raw_text:
            lines.append(f"<blockquote>{escape(event.raw_text[:800])}</blockquote>")
        if event.post_url:
            lines.append(f'<a href="{escape(event.post_url, quote=True)}">🔗 Пост</a>')

    if project.notes:
        lines.append("")
        lines.append(f"📝 <b>Заметка:</b> <i>{escape(project.notes[:1500])}</i>")

    return "\n".join(lines)


def _project_line(project: Project) -> str:
    kind = project.project_type.value if project.project_type else "?"
    extra = " 💰" if project.is_tier1_backed else ""
    return (
        f'<a href="https://x.com/{escape(project.handle)}">@{escape(project.handle)}</a>'
        f" · {kind} · score {project.score or 0:.0f}{extra}"
    )


def _open_keyboard(projects: list[Project]) -> InlineKeyboardMarkup | None:
    if not projects:
        return None
    rows = [
        [InlineKeyboardButton(text=f"@{p.handle}", callback_data=f"open:{p.id}")]
        for p in projects[:10]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _load_event(
    session: AsyncSession, project_id: int, event_id: int | None = None
) -> FeedEvent | None:
    """Loads the given event or, if not specified, the latest one for the project."""
    if event_id is not None:
        event = await session.get(FeedEvent, event_id)
    else:
        result = await session.execute(
            select(FeedEvent)
            .where(FeedEvent.project_id == project_id)
            .order_by(FeedEvent.created_at.desc())
            .limit(1)
        )
        event = result.scalar_one_or_none()
    if event is not None:
        await event.awaitable_attrs.smart_account
    return event


async def send_alert(bot: Bot, project_id: int, event_id: int | None = None) -> Message | None:
    """Sends a loud alert for the project to the admin. Entry point for the monitoring pipeline."""
    async with async_session() as session:
        project = await session.get(Project, project_id)
        if project is None:
            return None
        text = build_alert_text(project, await _load_event(session, project_id, event_id))

    return await bot.send_message(
        chat_id=settings.admin_id,
        text=text,
        reply_markup=build_alert_keyboard(project_id),
        disable_web_page_preview=True,
    )


def _split_callback(data: str) -> tuple[str, int]:
    action, raw_id = data.split(":", 1)
    return action, int(raw_id)


async def _refresh_alert(
    bot: Bot, chat_id: int | None, message_id: int | None, project: Project, event: FeedEvent | None
) -> None:
    if not chat_id or not message_id:
        return
    try:
        await bot.edit_message_text(
            text=build_alert_text(project, event),
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=build_alert_keyboard(project.id),
            disable_web_page_preview=True,
        )
    except TelegramBadRequest:
        pass


async def _send_project_list(message: Message, title: str, statuses: tuple[ProjectStatus, ...]) -> None:
    async with async_session() as session:
        rows = (
            await session.execute(
                select(Project)
                .where(Project.status.in_(statuses))
                .order_by(Project.score.desc(), Project.created_at.desc())
                .limit(20)
            )
        ).scalars().all()
    if not rows:
        await message.answer(f"{title} пуст.", reply_markup=main_keyboard())
        return
    text = f"<b>{title}</b>\n" + "\n".join(_project_line(p) for p in rows)
    await message.answer(text, reply_markup=_open_keyboard(rows), disable_web_page_preview=True)


# ---------- Menu ----------


@router.message(Command("start", "help"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(START_TEXT, reply_markup=main_keyboard())


@router.message(F.text == BTN_INBOX)
async def cmd_inbox(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _send_project_list(message, "Inbox", (ProjectStatus.INBOX,))


@router.message(F.text == BTN_HOLDING)
async def cmd_holding(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _send_project_list(message, "Holding", (ProjectStatus.HOLDING,))


@router.message(F.text == BTN_PRIORITY)
async def cmd_priority(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _send_project_list(message, "Priority", (ProjectStatus.HIGH_PRIORITY,))


@router.message(F.text == BTN_SMARTS)
async def cmd_smarts(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with async_session() as session:
        rows = await get_all_smart_accounts(session)
    if not rows:
        await message.answer("Смартов нет. Запусти seed_db.py.", reply_markup=main_keyboard())
        return
    lines = []
    for s in rows:
        marks = ("⚡" if s.is_fast_track else "") + ("👁" if s.is_active else "")
        lines.append(f"{marks or '⏸'} @{escape(s.handle)} · T{s.tier}")
    await message.answer("\n".join(lines), reply_markup=main_keyboard())


@router.message(F.text == BTN_DIGEST)
async def cmd_digest(message: Message, state: FSMContext) -> None:
    await state.clear()
    count = await send_digest(message.bot)
    await message.answer(f"Дайджест отправлен ({count} проектов).", reply_markup=main_keyboard())


@router.callback_query(F.data.regexp(r"^open:\d+$"))
async def on_open_project(callback: CallbackQuery, bot: Bot) -> None:
    _, project_id = _split_callback(callback.data)
    sent = await send_alert(bot, project_id)
    if sent is None:
        await callback.answer("Проект не найден", show_alert=True)
        return
    await callback.answer()


# ---------- Status buttons ----------


@router.callback_query(
    F.data.regexp(r"^(status_research|status_wl|status_holding|status_inbox|status_blacklist):\d+$")
)
async def on_status_change(callback: CallbackQuery, bot: Bot) -> None:
    action, project_id = _split_callback(callback.data)
    new_status = STATUS_CALLBACKS[action]

    async with async_session() as session:
        project = await session.get(Project, project_id)
        if project is None:
            await callback.answer("Проект не найден", show_alert=True)
            return
        project.status = new_status
        await session.commit()
        event = await _load_event(session, project_id)

    await callback.answer(f"@{project.handle} → {STATUS_LABELS[new_status]}")
    msg = callback.message
    await _refresh_alert(
        bot, msg.chat.id if msg else None, msg.message_id if msg else None, project, event
    )


# ---------- Fast-Track ----------

_admin_only = F.from_user.id == settings.admin_id


@router.message(Command("add"), _admin_only)
async def on_add(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Использование: /add @username")
        return
    async with async_session() as session:
        try:
            account, _ = await add_fast_track_account(session, command.args.split()[0])
        except ValueError:
            await message.answer("❌ Некорректный юзернейм X.")
            return
        await session.commit()
    await message.answer(
        f"✅ Аккаунт @{escape(account.handle)} добавлен в Fast-Track. "
        "Новые посты будут приходить моментально."
    )


@router.message(Command("remove"), _admin_only)
async def on_remove(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Использование: /remove @username")
        return
    async with async_session() as session:
        try:
            account = await remove_fast_track_account(session, command.args.split()[0])
        except ValueError:
            await message.answer("❌ Некорректный юзернейм X.")
            return
        await session.commit()
    if account is None:
        await message.answer("Этого аккаунта нет в Fast-Track.")
    elif account.is_active:
        await message.answer(
            f"🗑 @{escape(account.handle)} убран из Fast-Track (отслеживание подписок сохранено)."
        )
    else:
        await message.answer(f"🗑 @{escape(account.handle)} удалён из отслеживаемых.")


@router.message(Command("list"), _admin_only)
async def on_list(message: Message) -> None:
    async with async_session() as session:
        accounts = await get_all_smart_accounts(session)
    if not accounts:
        await message.answer("Список отслеживаемых аккаунтов пуст. Добавьте: /add @username")
        return
    fast = sum(a.is_fast_track for a in accounts)
    lines = [f"📋 Отслеживаемые аккаунты ({len(accounts)}, ⚡ Fast-Track: {fast})", ""]
    for a in accounts:
        marks = ("⚡" if a.is_fast_track else "") + ("👁" if a.is_active else "")
        lines.append(f"{marks or '⏸'} @{escape(a.handle)} · T{a.tier}")
    lines += ["", "⚡ Fast-Track · 👁 подписки · ⏸ выключен"]
    for chunk in _chunks("\n".join(lines)):
        await message.answer(chunk)


# ---------- Note FSM ----------


@router.callback_query(F.data.regexp(r"^note:\d+$"))
async def on_note_request(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    _, project_id = _split_callback(callback.data)

    async with async_session() as session:
        project = await session.get(Project, project_id)
    if project is None:
        await callback.answer("Проект не найден", show_alert=True)
        return

    msg = callback.message
    await state.set_state(NoteStates.waiting_for_text)
    await state.update_data(
        project_id=project_id,
        alert_chat_id=msg.chat.id if msg else None,
        alert_message_id=msg.message_id if msg else None,
    )
    await callback.answer()
    await bot.send_message(
        chat_id=callback.from_user.id,
        text=(
            f"📝 Напишите заметку для <b>@{escape(project.handle)}</b>.\n"
            "Следующее сообщение будет сохранено. /cancel — отмена."
        ),
        reply_markup=main_keyboard(),
    )


@router.message(StateFilter(NoteStates.waiting_for_text), Command("cancel"))
async def on_note_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Заметка отменена.", reply_markup=main_keyboard())


@router.message(StateFilter(NoteStates.waiting_for_text), F.text)
async def on_note_text(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    project_id = data.get("project_id")
    await state.clear()

    async with async_session() as session:
        project = await session.get(Project, project_id) if project_id else None
        if project is None:
            await message.answer("Проект не найден, заметка не сохранена.", reply_markup=main_keyboard())
            return
        project.notes = message.text
        await session.commit()
        event = await _load_event(session, project_id)

    await message.answer(
        f"✅ Заметка для <b>@{escape(project.handle)}</b> сохранена.",
        reply_markup=main_keyboard(),
    )
    await _refresh_alert(
        bot, data.get("alert_chat_id"), data.get("alert_message_id"), project, event
    )


@router.message(StateFilter(NoteStates.waiting_for_text))
async def on_note_non_text(message: Message) -> None:
    await message.answer("Нужен текст. Напишите заметку или /cancel для отмены.")
