"""Витягання тексту з PDF, сторінок і пошук посилань."""
from __future__ import annotations

from triage.extract import extract_pdf_text, fetch_page_text, find_links
from triage.models import Attachment

from .fakes import FakeHttpResponse, FakeSession
from .fixtures.build_pdf import corrupt_pdf, make_pdf


def _att(data: bytes, name: str = "doc.pdf") -> Attachment:
    return Attachment(filename=name, mime_type="application/pdf", size=len(data), data=data)


class TestPdf:
    def test_reads_text(self):
        pdf = make_pdf(["INVOICE 2026-0815", "Total: 48000 UAH", "Due: 2026-08-29"])
        doc = extract_pdf_text(_att(pdf))
        assert doc.error is None
        assert "INVOICE 2026-0815" in doc.text
        assert "48000" in doc.text

    def test_corrupt_pdf_reports_error_instead_of_crashing(self):
        doc = extract_pdf_text(_att(corrupt_pdf()))
        assert doc.error is not None
        assert doc.text == ""

    def test_empty_attachment(self):
        doc = extract_pdf_text(Attachment("x.pdf", "application/pdf", 0, None))
        assert doc.error == "порожнє вкладення"

    def test_truncates_long_text(self):
        pdf = make_pdf([f"line number {i} with some padding text" for i in range(200)])
        doc = extract_pdf_text(_att(pdf), max_chars=300)
        assert doc.truncated is True
        assert len(doc.text) <= 300


class TestLinks:
    def test_finds_links_in_order(self):
        text = "дивіться https://example.com/a та https://example.org/b"
        assert find_links(text) == ["https://example.com/a", "https://example.org/b"]

    def test_skips_tracking_and_unsubscribe(self):
        text = "https://click.mailer.io/x https://example.com/real https://list-manage.com/u"
        assert find_links(text) == ["https://example.com/real"]

    def test_respects_limit(self):
        text = " ".join(f"https://site{i}.com/p" for i in range(10))
        assert len(find_links(text, limit=3)) == 3

    def test_strips_trailing_punctuation(self):
        assert find_links("ось посилання https://example.com/page.") == [
            "https://example.com/page"
        ]


class TestWebSsrfGuard:
    """Посилання пише відправник листа, тому внутрішня мережа має бути закрита."""

    def test_blocks_loopback(self):
        doc = fetch_page_text("http://127.0.0.1/admin")
        assert "внутрішн" in (doc.error or "")

    def test_blocks_private_range(self):
        doc = fetch_page_text("http://10.0.0.5/secret")
        assert "внутрішн" in (doc.error or "")

    def test_blocks_cloud_metadata_endpoint(self):
        doc = fetch_page_text("http://169.254.169.254/latest/meta-data/")
        assert "внутрішн" in (doc.error or "")

    def test_blocks_non_http_scheme(self):
        doc = fetch_page_text("file:///etc/passwd")
        assert "схема" in (doc.error or "")


class TestWebFetch:
    def test_extracts_visible_text(self, monkeypatch):
        monkeypatch.setattr(
            "triage.extract.web._resolves_to_private", lambda host: False
        )
        html = (
            "<html><head><title>Умови оплати</title><style>b{}</style></head>"
            "<body><script>x()</script><h1>Тариф Pro</h1>"
            "<p>Вартість 500 доларів на місяць.</p></body></html>"
        )
        session = FakeSession({"https://example.com/pricing": FakeHttpResponse(html)})
        doc = fetch_page_text("https://example.com/pricing", session=session)
        assert doc.error is None
        assert "Умови оплати" in doc.text
        assert "500 доларів" in doc.text
        assert "x()" not in doc.text  # скрипти вирізані

    def test_rejects_non_html_content(self, monkeypatch):
        monkeypatch.setattr(
            "triage.extract.web._resolves_to_private", lambda host: False
        )
        session = FakeSession(
            {"https://example.com/f.zip": FakeHttpResponse("", content_type="application/zip")}
        )
        doc = fetch_page_text("https://example.com/f.zip", session=session)
        assert "не сторінка" in (doc.error or "")

    def test_connection_failure_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(
            "triage.extract.web._resolves_to_private", lambda host: False
        )
        session = FakeSession({})
        doc = fetch_page_text("https://example.com/missing", session=session)
        assert doc.error is not None
        assert doc.text == ""
