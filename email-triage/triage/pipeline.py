"""Оркестрація: лист → матеріали → класифікація → звіт.

Жодна помилка на одному листі не зупиняє прогін. Лист, який не вдалося
обробити, отримує вердикт NEEDS_HUMAN_REVIEW і потрапляє у звіт — мовчки
не зникає ніколи.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .classifier import REVIEW, Classifier, ClassifierError
from .config import Config
from .errorlog import ErrorLog
from .extract import extract_pdf_text, fetch_page_text, find_links
from .models import EmailMessage, ExtractedDoc, TriageResult, Verdict
from .prefilter import prefilter
from .state import State

PDF_MIME_HINTS = ("application/pdf", "application/x-pdf", "application/octet-stream")


@dataclass
class RunReport:
    fetched: int = 0
    skipped_seen: int = 0
    prefiltered: int = 0
    processed: int = 0
    written: int = 0
    labels: Counter = field(default_factory=Counter)
    errors: int = 0
    sink_description: str = ""

    def render(self) -> str:
        lines = [
            "",
            "═" * 58,
            "ПІДСУМОК ПРОГОНУ",
            "═" * 58,
            f"Знайдено листів:        {self.fetched}",
            f"Пропущено (вже були):   {self.skipped_seen}",
            f"Відсіяно як розсилку:   {self.prefiltered}",
            f"Оброблено:              {self.processed}",
            f"Записано у звіт:        {self.written}",
            "",
            f"  🔴 IMPORTANT           {self.labels.get('IMPORTANT', 0)}",
            f"  ⚪ NOT_IMPORTANT       {self.labels.get('NOT_IMPORTANT', 0)}",
            f"  🟡 NEEDS_HUMAN_REVIEW  {self.labels.get(REVIEW, 0)}",
            "",
            f"Помилок:                {self.errors}",
            f"Результат:              {self.sink_description}",
            "═" * 58,
        ]
        return "\n".join(lines)


def _looks_like_pdf(attachment) -> bool:
    if attachment.mime_type in PDF_MIME_HINTS:
        return True
    return attachment.filename.lower().endswith(".pdf")


def gather_documents(
    email: EmailMessage, cfg: Config, errlog: ErrorLog, *, session=None
) -> list[ExtractedDoc]:
    """Збирає текст із PDF-вкладень і сторінок за посиланнями."""
    docs: list[ExtractedDoc] = []

    if cfg.read_pdfs:
        for attachment in email.attachments:
            if not _looks_like_pdf(attachment):
                continue
            doc = extract_pdf_text(
                attachment, max_pages=cfg.max_pdf_pages, max_chars=cfg.max_doc_chars
            )
            docs.append(doc)
            if doc.error:
                errlog.record(
                    "pdf", f"{attachment.filename}: {doc.error}",
                    message_id=email.message_id,
                )

    if cfg.fetch_links:
        for url in find_links(email.body, limit=cfg.max_links_per_email):
            doc = fetch_page_text(
                url,
                timeout=cfg.link_timeout_seconds,
                max_bytes=cfg.max_download_bytes,
                max_chars=cfg.max_doc_chars,
                session=session,
            )
            docs.append(doc)
            if doc.error:
                errlog.record(
                    "link", f"{url}: {doc.error}", message_id=email.message_id
                )
    return docs


def degraded_verdict(email: EmailMessage, reason: str) -> Verdict:
    """Вердикт для листа, який не вдалося класифікувати."""
    return Verdict(
        label=REVIEW,
        confidence=0.0,
        reason=f"Не вдалося класифікувати автоматично: {reason}",
        sender_summary=email.sender,
        topic=email.subject,
        action_required="Переглянути вручну",
        degraded=True,
    )


def run(
    cfg: Config,
    source,
    classifier: Classifier,
    sink,
    state: State,
    errlog: ErrorLog,
    *,
    limit: int | None = None,
    batch_size: int = 10,
    session=None,
) -> RunReport:
    report = RunReport(sink_description=sink.describe())

    try:
        emails = source.fetch(limit or cfg.max_emails_per_run)
    except Exception as exc:
        errlog.record("gmail", f"не вдалося отримати листи: {exc}", exc=exc, fatal=True)
        report.errors = errlog.total
        return report

    report.fetched = len(emails)
    pending: list[TriageResult] = []

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        try:
            report.written += sink.write(pending)
            # Позначаємо як оброблені лише після успішного запису — інакше
            # лист зник би зі звіту назавжди після збою приймача.
            for result in pending:
                state.mark(result.email.message_id, result.verdict.label)
        except Exception as exc:
            errlog.record(
                "sink", f"не вдалося записати {len(pending)} рядків: {exc}", exc=exc
            )
        pending = []

    for email in emails:
        if state.seen(email.message_id):
            report.skipped_seen += 1
            continue
        # Дешевий фільтр іде першим: він економить і гроші, і час на
        # завантаження вкладень та сторінок.
        cheap = prefilter(email, cfg.never_skip_senders) if cfg.skip_bulk_mail else None
        if cheap is not None:
            pending.append(TriageResult(email=email, verdict=cheap, docs=[]))
            report.processed += 1
            report.prefiltered += 1
            report.labels[cheap.label] += 1
            if len(pending) >= batch_size:
                flush()
            continue

        try:
            docs = gather_documents(email, cfg, errlog, session=session)
        except Exception as exc:
            errlog.record(
                "extract", f"збій витягу матеріалів: {exc}",
                message_id=email.message_id, exc=exc,
            )
            docs = []
        try:
            verdict = classifier.classify(email, docs)
        except ClassifierError as exc:
            errlog.record(
                "classify", str(exc), message_id=email.message_id, exc=exc
            )
            verdict = degraded_verdict(email, str(exc))
        except Exception as exc:
            errlog.record(
                "classify", f"неочікувана помилка: {exc}",
                message_id=email.message_id, exc=exc,
            )
            verdict = degraded_verdict(email, f"{type(exc).__name__}: {exc}")

        pending.append(TriageResult(email=email, verdict=verdict, docs=docs))
        report.processed += 1
        report.labels[verdict.label] += 1
        if len(pending) >= batch_size:
            flush()

    flush()
    report.errors = errlog.total
    return report
