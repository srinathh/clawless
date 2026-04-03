"""Tests for clawless.utils."""

from clawless.utils import split_text


def test_empty_string():
    assert split_text("", 100) == []


def test_short_string():
    assert split_text("hello", 100) == ["hello"]


def test_exact_length():
    text = "a" * 100
    assert split_text(text, 100) == [text]


def test_split_at_newline():
    text = "first line\nsecond line"
    chunks = split_text(text, 15)
    assert chunks == ["first line", "second line"]


def test_split_at_space():
    text = "hello world foo"
    chunks = split_text(text, 11)
    # rfind(" ") finds the last space in the 11-char window "hello world"
    assert chunks[0] == "hello"
    assert chunks[1] == "world foo"


def test_hard_cut_no_break():
    text = "a" * 200
    chunks = split_text(text, 100)
    assert len(chunks) == 2
    assert len(chunks[0]) == 100
    assert len(chunks[1]) == 100


def test_multiple_chunks():
    text = "line one\nline two\nline three\nline four"
    chunks = split_text(text, 18)
    assert all(len(c) <= 18 for c in chunks)
    # Splits at newlines; lstrip removes leading whitespace between chunks
    assert len(chunks) >= 2
