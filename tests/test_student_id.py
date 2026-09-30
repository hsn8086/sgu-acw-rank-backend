"""Tests for student id parsing.

The sample ids come from the sgu-aio documentation and from the public listings
it was reverse-engineered from, so they double as a check that the rules did not
drift.
"""

from __future__ import annotations

import pytest

from app.student_id import (
    StudentIdError,
    is_valid_student_id,
    parse_student_id,
)


@pytest.mark.parametrize(
    ("raw", "year", "level", "college", "major", "class_no", "serial"),
    [
        # Documented example: 2024 级软件工程.
        ("24125023005", 2024, 1, "25", "02", 3, 5),
        # The documented anomaly: a 2021 id sitting in a 2024 class.
        ("21117021032", 2021, 1, "17", "02", 1, 32),
        # 专科 (level 2): 21|2|51|04|4|001.
        ("21251044001", 2021, 2, "51", "04", 4, 1),
        # 专升本 (level 3): 23|3|03|01|1|001.
        ("23303011001", 2023, 3, "03", "01", 1, 1),
    ],
)
def test_parse_valid(raw, year, level, college, major, class_no, serial):
    parsed = parse_student_id(raw)
    assert parsed.enroll_year == year
    assert parsed.level == level
    assert parsed.college_code == college
    assert parsed.major_code == major
    assert parsed.class_no == class_no
    assert parsed.serial == serial
    assert parsed.link_letter is None
    assert not parsed.is_linked


def test_parse_normalises_case_and_whitespace():
    parsed = parse_student_id("  24125023005  ")
    assert parsed.raw == "24125023005"


def test_parse_joint_programme_twelve_digit():
    # 24|1|95|04|1|001|H — 普通本科 joint programme at college 95.
    parsed = parse_student_id("24195041001H")
    assert parsed.is_linked
    assert parsed.link_letter == "H"
    assert parsed.college_code == "95"
    assert parsed.raw == "24195041001H"


@pytest.mark.parametrize(
    "raw",
    [
        "",  # empty
        "2412502300",  # one digit short
        "241250230055",  # one digit too many
        "2412502300X",  # letter on an 11-digit id
        "2412502300A",  # same, different letter
        "99999999999",  # college code 99 is not real
        "24199023005",  # college code 99 in position
        "24525023005",  # level 5 does not exist
        "24025023005",  # level 0 does not exist
        "2412502300!",  # punctuation
        "abcdefghijk",  # not digits
    ],
)
def test_parse_rejects(raw):
    with pytest.raises(StudentIdError):
        parse_student_id(raw)


def test_linked_college_requires_letter():
    """A 94-97 college code without the trailing letter is malformed."""
    with pytest.raises(StudentIdError):
        parse_student_id("24195041001")


def test_plain_college_rejects_letter():
    """A trailing letter on a normal college code is malformed."""
    with pytest.raises(StudentIdError):
        parse_student_id("24125023005H")


def test_is_valid_helper():
    assert is_valid_student_id("24125023005")
    assert not is_valid_student_id("2412502300")


def test_level_name():
    assert parse_student_id("24125023005").level_name == "普通本科"
    assert parse_student_id("21251044001").level_name == "专科"
    assert parse_student_id("23303011001").level_name == "专升本/协同育人"
