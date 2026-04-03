"""Shared utility functions."""

from __future__ import annotations


def split_text(text: str, max_len: int) -> list[str]:
    """Split text into chunks of at most max_len characters.

    Prefers splitting at newlines, then spaces, to keep paragraphs and
    sentences intact. Falls back to hard-cutting at max_len if no
    suitable break point is found.
    """
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        cut = text[:max_len]
        pos = cut.rfind("\n")
        if pos <= 0:
            pos = cut.rfind(" ")
        if pos <= 0:
            pos = max_len
        chunks.append(text[:pos])
        text = text[pos:].lstrip()
    return chunks
