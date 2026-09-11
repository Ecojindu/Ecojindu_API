"""The conversational parsers must be forgiving — passengers type on phones."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.whatsapp.flow import (
    extract_booking_ref,
    looks_like_name,
    parse_choice,
    parse_count,
    parse_date,
    parse_email,
)

LAGOS = ZoneInfo("Africa/Lagos")


def today() -> date:
    return datetime.now(LAGOS).date()


@pytest.mark.parametrize(
    "text,expected_offset",
    [
        ("today", 0), ("TODAY", 0), ("  today  ", 0), ("tod", 0),
        ("tomorrow", 1), ("Tomorrow", 1), ("tmrw", 1), ("tomorow", 1),
        ("2moro", 1), ("tmr", 1), ("tomorrw", 1),
        ("day after tomorrow", 2),
    ],
)
def test_relative_dates_and_common_typos(text, expected_offset):
    assert parse_date(text) == today() + timedelta(days=expected_offset)


def test_weekday_names_resolve_forward():
    for word in ("monday", "mon", "friday", "fri", "sun"):
        parsed = parse_date(word)
        assert parsed is not None
        assert parsed > today()
        assert (parsed - today()).days <= 7


def test_explicit_date_formats():
    target = today() + timedelta(days=5)
    assert parse_date(target.strftime("%Y-%m-%d")) == target
    assert parse_date(target.strftime("%d/%m/%Y")) == target
    assert parse_date(target.strftime("%d-%m-%Y")) == target


def test_unparseable_dates_return_none():
    for junk in ("", "sometime", "next month maybe", "abc", "99/99/9999"):
        assert parse_date(junk) is None


@pytest.mark.parametrize(
    "text,expected",
    [("1", 1), ("2", 2), ("two", 2), ("TWO", 2), ("a couple", 2), ("3 seats", 3),
     ("I need 4", 4), ("one", 1), ("single", 1)],
)
def test_seat_counts(text, expected):
    assert parse_count(text, 6) == expected


def test_seat_counts_out_of_range():
    assert parse_count("0", 6) is None
    assert parse_count("9", 6) is None
    assert parse_count("no idea", 6) is None


@pytest.mark.parametrize("text,expected", [("1", 0), ("2.", 1), ("option 3", 2), ("#2", 1), (" 4 ", 3)])
def test_menu_choices_are_zero_indexed(text, expected):
    assert parse_choice(text, 4) == expected


def test_menu_choice_out_of_range():
    assert parse_choice("7", 4) is None
    assert parse_choice("zero", 4) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("EJS-8K3F2", "EJS-8K3F2"),
        ("ejs-8k3f2", "EJS-8K3F2"),
        ("my booking EJS-8K3F2 please", "EJS-8K3F2"),
        ("EJS 8K3F2", "EJS-8K3F2"),
    ],
)
def test_booking_ref_extraction(text, expected):
    assert extract_booking_ref(text) == expected


def test_booking_ref_absent():
    assert extract_booking_ref("hello there") is None


def test_name_validation():
    assert looks_like_name("Chinedu Okafor")
    assert looks_like_name("Ngozi Eze-Nwosu")
    assert looks_like_name("O'Brien Adeyemi")
    assert not looks_like_name("X")
    assert not looks_like_name("08154471570")
    assert not looks_like_name("")


def test_email_parsing():
    assert parse_email("ada@example.com") == "ada@example.com"
    assert parse_email("  ADA@Example.COM ") == "ada@example.com"
    assert parse_email("skip") is None
    assert parse_email("SKIP") is None
    assert parse_email("no") is None
    assert parse_email("not-an-email") == "invalid"
