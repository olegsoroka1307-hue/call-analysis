"""CLI: перевіряємо розбір аргументів і команди, які не ходять у мережу."""
from __future__ import annotations

import pytest

from run import build_parser, cmd_people, cmd_register


def parse(argv: list[str]):
    return build_parser().parse_args(argv)


def test_every_command_from_the_readme_exists():
    for argv in (
        ["analyze", "narada.txt"],
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
