from __future__ import annotations

from meetings.registry import transcript_fingerprint


def test_find_by_full_name_and_username(registry):
    registry.add("Саша Петренко", 111, "sasha_p")
    assert registry.find("Саша Петренко").chat_id == 111
    assert registry.find("sasha_p").chat_id == 111
    assert registry.find("@sasha_p").chat_id == 111


def test_find_ignores_case_and_spaces(registry):
    registry.add("Марія Коваль", 222)
    assert registry.find("  марія коваль ").chat_id == 222


def test_short_name_from_the_meeting_matches_full_name(registry):
    # На нараді кажуть «Саша», у реєстрі «Саша Петренко».
    registry.add("Саша Петренко", 111)
    assert registry.find("Саша").chat_id == 111


def test_two_people_with_the_same_first_name_are_not_guessed(registry):
    # Написати «якомусь Саші» гірше, ніж не написати нікому: задача піде
    # не тій людині, і ніхто цього не помітить.
    registry.add("Саша Петренко", 111)
    registry.add("Саша Іванов", 112)
    assert registry.find("Саша") is None
    assert registry.find("Саша Петренко").chat_id == 111


def test_unknown_person_is_none(registry):
    assert registry.find("Хтось") is None
    assert registry.find("") is None


def test_adding_again_updates_chat_id(registry):
    registry.add("Петро", 333)
    registry.add("Петро", 444)
    assert registry.find("Петро").chat_id == 444


def test_all_lists_each_person_once(registry):
    registry.add("Саша Петренко", 111, "sasha_p")
    registry.add("Марія", 222)
    assert sorted(p.name for p in registry.all()) == ["Марія", "Саша Петренко"]


def test_remove(registry):
    registry.add("Саша Петренко", 111, "sasha_p")
    assert registry.remove("sasha_p") is True
    assert registry.find("Саша Петренко") is None
    assert registry.remove("Саша Петренко") is False


def test_meeting_is_remembered(registry):
    fingerprint = transcript_fingerprint("Саша: зроблю КП.")
    assert registry.seen_meeting(fingerprint) is False
    registry.mark_meeting(fingerprint, "Планірка", 3)
    assert registry.seen_meeting(fingerprint) is True


def test_fingerprint_ignores_surrounding_whitespace():
    assert transcript_fingerprint(" текст \n") == transcript_fingerprint("текст")
    assert transcript_fingerprint("текст") != transcript_fingerprint("інший текст")
