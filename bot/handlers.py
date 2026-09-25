"""Alert rendering, inline status buttons and the note FSM."""

from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from core.database import async_session
from core.models import FeedEvent, Project, ProjectStatus

router = Router(name="alerts")

STATUS_CALLBACKS: dict[str, ProjectStatus] = {
    "status_research": ProjectStatus.RESEARCH,
    "status_wl": ProjectStatus.WL_HUNT,
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


# ---------- Rendering ----------


def build_alert_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔍 Research", callback_data=f"status_research:{project_id}"),
                InlineKeyboardButton(text="🎯 WL-Hunt", callback_data=f"status_wl:{project_id}"),
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
        # "message is not modified" or the message is too old to edit.
        pass


# ---------- Status buttons ----------


@router.callback_query(F.data.regexp(r"^(status_research|status_wl|status_blacklist):\d+$"))
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
    )


@router.message(StateFilter(NoteStates.waiting_for_text), Command("cancel"))
async def on_note_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Заметка отменена.")


@router.message(StateFilter(NoteStates.waiting_for_text), F.text)
async def on_note_text(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    project_id = data.get("project_id")
    await state.clear()

    async with async_session() as session:
        project = await session.get(Project, project_id) if project_id else None
        if project is None:
            await message.answer("Проект не найден, заметка не сохранена.")
            return
        project.notes = message.text
        await session.commit()
        event = await _load_event(session, project_id)

    await message.answer(f"✅ Заметка для <b>@{escape(project.handle)}</b> сохранена.")
    await _refresh_alert(
        bot, data.get("alert_chat_id"), data.get("alert_message_id"), project, event
    )


@router.message(StateFilter(NoteStates.waiting_for_text))
async def on_note_non_text(message: Message) -> None:
    await message.answer("Нужен текст. Напишите заметку или /cancel для отмены.")
