"""CLI: перевіряємо розбір аргументів і команди, які не ходять у мережу."""
from __future__ import annotations

import pytest

from run import _read_transcript, build_parser, cmd_document, cmd_people, cmd_register


def parse(argv: list[str]):
    return build_parser().parse_args(argv)


def test_every_command_from_the_readme_exists():
    for argv in (
        ["analyze", "narada.txt"],
        ["document", "narada.txt"],
        ["report"],
        ["listen"],
        ["register"],
        ["people"],
        ["setup", "page-1"],
    ):
        assert parse(argv).command == argv[0]


def test_analyze_flags():
    args = parse(["analyze", "narada.txt", "--name", "Планірка", "--dry-run", "--force"])
    assert (args.transcript, args.name) == ("narada.txt", "Планірка")
    assert args.dry_run is True and args.force is True


def test_register_without_a_name_is_the_contact_listing():
    args = parse(["register"])
    assert args.name == "" and args.chat_id is None


def test_register_adds_a_person(cfg, capsys):
    args = parse(["register", "Саша Петренко", "--chat-id", "111", "--username", "sasha_p"])
    assert cmd_register(cfg, args) == 0
    assert "Додано: Саша Петренко" in capsys.readouterr().out

    assert cmd_people(cfg, parse(["people"])) == 0
    out = capsys.readouterr().out
    assert "Саша Петренко" in out and "@sasha_p" in out and "111" in out


def test_register_without_chat_id_explains_instead_of_guessing(cfg, capsys):
    # Вигадати chat_id не можна: повідомлення пішло б чужій людині.
    args = parse(["register", "Саша"])
    assert cmd_register(cfg, args) == 2
    assert "--chat-id" in capsys.readouterr().err


def test_empty_registry_points_at_the_next_step(cfg, capsys):
    assert cmd_people(cfg, parse(["people"])) == 0
    assert "run.py register" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["невідома-команда"]])
def test_bad_command_is_refused(argv):
    with pytest.raises(SystemExit):
        parse(argv)


# ── document ────────────────────────────────────────────────────────
def test_document_flags():
    args = parse([
        "document", "narada.txt", "--kind", "методичка",
        "--what", "як готувати КП", "--name", "Планірка",
    ])
    assert args.kind == "методичка"
    assert args.what == "як готувати КП"
    assert args.name == "Планірка"


def test_document_needs_a_readable_transcript(cfg, tmp_path, capsys):
    args = parse(["document", str(tmp_path / "нема.txt"), "--what", "x", "--kind", "кп"])
    assert cmd_document(cfg, args) == 2
    assert "Немає файлу" in capsys.readouterr().err


def test_document_without_what_stops_when_it_cannot_ask(cfg, tmp_path, capsys, monkeypatch):
    # У скрипті чи в пайпі спитати нікого: краще сказати про це, ніж
    # вигадати за власника, що саме зробити з наради.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    path = tmp_path / "narada.txt"
    path.write_text("Саша: почнемо з бюджету.", encoding="utf-8")

    args = parse(["document", str(path), "--kind", "кп"])
    assert cmd_document(cfg, args) == 2
    assert "--what" in capsys.readouterr().err


def test_document_without_kind_stops_when_it_cannot_ask(cfg, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    path = tmp_path / "narada.txt"
    path.write_text("Саша: почнемо з бюджету.", encoding="utf-8")

    args = parse(["document", str(path), "--what", "шаблон"])
    assert cmd_document(cfg, args) == 2
    err = capsys.readouterr().err
    assert "--kind" in err and "методичка" in err


# ── читання транскрипції ────────────────────────────────────────────
def test_transcript_is_read_from_a_file(tmp_path):
    path = tmp_path / "narada.txt"
    path.write_text("Саша: почнемо з бюджету.", encoding="utf-8")
    assert _read_transcript(str(path)) == "Саша: почнемо з бюджету."


def test_missing_file_is_reported(tmp_path, capsys):
    assert _read_transcript(str(tmp_path / "нема.txt")) is None
    assert "Немає файлу" in capsys.readouterr().err


def test_blank_transcript_is_refused(tmp_path, capsys):
    path = tmp_path / "porozhnya.txt"
    path.write_text("   \n\n", encoding="utf-8")
    assert _read_transcript(str(path)) is None
    assert "порожня" in capsys.readouterr().err


def test_blank_stdin_is_refused_too(monkeypatch, capsys):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("  \n"))
    assert _read_transcript("-") is None
    assert "порожня" in capsys.readouterr().err
