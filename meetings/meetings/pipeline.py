"""Оркестрація: транскрипція → задачі в Notion → розсилка в Telegram.

Правило те саме, що й у системі тріажу пошти: збій в одній ланці не має
знищувати роботу решти. Якщо Notion недоступний — задачі все одно йдуть
людям, просто без кнопок. Якщо Telegram мовчить — задачі все одно створені.
Мовчки не зникає ніщо: усе, що не спрацювало, потрапляє в підсумок.
"""
from __future__ import annotations

from datetime import date

from .config import Config
from .errorlog import ErrorLog
from .extractor import Extractor, ExtractorError
from .models import MeetingResult
from .notion import NotionError
from .registry import Registry, transcript_fingerprint
from .telegram import TelegramError, restore_uuid, unpack_callback


def process_meeting(
    cfg: Config,
    transcript: str,
    extractor: Extractor,
    registry: Registry,
    errlog: ErrorLog,
    *,
    notion=None,
    telegram=None,
    meeting: str = "",
    today: date | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> MeetingResult:
    today = today or date.today()
    fingerprint = transcript_fingerprint(transcript)

    if not force and registry.seen_meeting(fingerprint):
        return MeetingResult(meeting=meeting or "(вже розібрана нарада)",
                             skipped_duplicate=True)

    try:
        title, commitments, unclear = extractor.extract(
            transcript, meeting=meeting, today=today
        )
    except ExtractorError as exc:
        errlog.record("extract", str(exc), exc)
        result = MeetingResult(meeting=meeting or "Нерозібрана нарада", degraded=True)
        result.degraded_reason = f"не вдалося розібрати транскрипцію: {exc}"
        return result

    result = MeetingResult(meeting=title, commitments=commitments, unclear=unclear)

    if dry_run:
        result.degraded = True
        result.degraded_reason = "пробний запуск: нічого не створено й не надіслано"
        return result

    # Кожна ланка, що спрацювала не повністю, лишає тут свій рядок. Разом вони
    # й утворюють попередження в підсумку — жоден збій не гасить попередній.
    problems: list[str] = []

    # ── Notion ───────────────────────────────────────────────────────
    if notion is not None:
        failed = 0
        for commitment in commitments:
            try:
                commitment.page_id = notion.create_task(commitment)
                result.created_in_notion += 1
            except NotionError as exc:
                failed += 1
                errlog.record("notion", f"{commitment.task}: {exc}", exc)
        if failed:
            problems.append(
                f"не створено задач у Notion: {failed} — див. журнал збоїв"
            )
        # Задача записана, але не такою, як її зняли з наради: не завівся
        # проєкт, підмінився пріоритет. Це не збій, але людина має це побачити.
        notes = notion.take_notes()
        for note in notes:
            errlog.record("notion", note)
        if notes:
            problems.append(f"записано з поправками: {len(notes)} — див. журнал збоїв")
    elif commitments:
        problems.append(
            "Notion не налаштований: задачі нікуди не записані, кнопки не працюватимуть"
        )

    # ── Telegram ─────────────────────────────────────────────────────
    if telegram is not None and cfg.send_telegram and commitments:
        try:
            result.deliveries = telegram.send_meeting_summary(title, commitments, registry)
        except TelegramError as exc:
            errlog.record("telegram", str(exc), exc)
            problems.append(f"розсилка не відбулася: {exc}")
        for delivery in result.unreached:
            errlog.record("telegram", f"{delivery.name}: {delivery.error}")
        if result.unreached:
            # Створена, але не надіслана задача — найгірший зі станів: у звіті
            # вона є, а людина про неї не знає. Це має бути видно одразу.
            names = ", ".join(d.name for d in result.unreached)
            problems.append(f"задачі не дійшли до: {names}")

    # Нарада вважається розібраною лише тоді, коли результат десь осів:
    # сторінками в Notion, а якщо Notion не налаштований — доставленими
    # повідомленнями. Інакше повторний запуск мовчки пропустив би нараду, і
    # задачі зникли б разом зі збоєм — це найдорожча помилка з можливих.
    if not commitments:
        persisted = True
    elif notion is not None:
        persisted = result.created_in_notion > 0
    else:
        persisted = any(delivery.reached for delivery in result.deliveries)

    if persisted:
        registry.mark_meeting(fingerprint, title, len(commitments))
    else:
        problems.append(
            "нічого не збережено — нараду не позначено розібраною, "
            "запустіть ту саму команду ще раз"
        )

    if problems:
        result.degraded = True
        result.degraded_reason = "; ".join(problems)
    return result


def handle_callbacks(telegram, notion, errlog: ErrorLog, *, offset: int | None = None,
                     timeout: int = 25) -> tuple[int, int | None]:
    """Один прохід по натисканнях кнопок. Повертає (оброблено, новий offset)."""
    try:
        updates = telegram.get_updates(offset, timeout=timeout)
    except TelegramError as exc:
        errlog.record("telegram", f"не вдалося отримати оновлення: {exc}", exc)
        return 0, offset

    handled = 0
    for update in updates:
        offset = update.get("update_id", 0) + 1
        query = update.get("callback_query")
        if not query:
            continue

        status, compact_id = unpack_callback(query.get("data", ""))
        if not status:
            continue

        message = query.get("message") or {}
        try:
            notion.set_status(restore_uuid(compact_id), status)
        except NotionError as exc:
            errlog.record("notion", f"статус {status}: {exc}", exc)
            _answer(telegram, errlog, query, "Не вдалося оновити — спробуйте пізніше")
            continue

        handled += 1
        _answer(telegram, errlog, query, f"Записав: {status}")
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        if chat_id is not None and message_id is not None:
            # Прибираємо кнопки, щоб не було двох натискань на ту саму задачу.
            try:
                telegram.clear_keyboard(chat_id, message_id)
            except TelegramError as exc:
                errlog.record("telegram", f"не вдалося прибрати кнопки: {exc}", exc)
    return handled, offset


def _answer(telegram, errlog: ErrorLog, query: dict, text: str) -> None:
    try:
        telegram.answer_callback(query.get("id", ""), text)
    except TelegramError as exc:
        errlog.record("telegram", f"не вдалося відповісти на натискання: {exc}", exc)
