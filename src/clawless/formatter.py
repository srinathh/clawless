"""Convert Claude markdown output to WhatsApp-compatible formatting."""

from __future__ import annotations

import re

TWILIO_MAX_MESSAGE_LEN = 1600


def format_for_whatsapp(text: str) -> str:
    """Convert markdown to WhatsApp format.

    - ``## Header`` → ``*Header*``
    - ``**bold**`` → ``*bold*``
    - ``- bullet`` → ``• bullet``
    - Strip HTML tags
    - Preserve code blocks
    """
    lines = text.split("\n")
    result: list[str] = []
    in_code_block = False

    for line in lines:
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            result.append(line)
            continue

        if in_code_block:
            result.append(line)
            continue

        # Headers → bold
        line = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", line)

        # **bold** → *bold* (but not inside code)
        line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)

        # Bullet points
        line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)

        # Strip HTML tags
        line = re.sub(r"<[^>]+>", "", line)

        result.append(line)

    return "\n".join(result)


def split_message(content: str, max_len: int = TWILIO_MAX_MESSAGE_LEN) -> list[str]:
    """Split content into chunks within max_len, preferring line breaks.

    Ported from nanobot utils/helpers.py.
    """
    if not content:
        return []
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break
        cut = content[:max_len]
        pos = cut.rfind("\n")
        if pos <= 0:
            pos = cut.rfind(" ")
        if pos <= 0:
            pos = max_len
        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks
