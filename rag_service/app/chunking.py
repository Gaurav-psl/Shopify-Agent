"""Heading-aware chunking: a chunk never straddles an unrelated section, and each chunk remembers
its heading path ('Refund policy > Eligibility'), which is also fed to the embedder."""
import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_SEPS = ["\n\n", "\n", ". ", " "]


@dataclass
class Chunk:
    body: str
    section: str


def _split(text: str, size: int, seps: list[str]) -> list[str]:
    if len(text) <= size:
        return [text]
    if not seps:
        return [text[i:i + size] for i in range(0, len(text), size)]
    sep, rest = seps[0], seps[1:]
    parts = text.split(sep)
    if len(parts) == 1:
        return _split(text, size, rest)
    out, cur = [], ""
    for i, p in enumerate(parts):
        piece = (sep if i else "") + p
        if len(piece) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.extend(_split(piece, size, rest))
        elif len(cur) + len(piece) <= size:
            cur += piece
        else:
            out.append(cur)
            cur = piece
    if cur:
        out.append(cur)
    return out


def _sections(md: str) -> list[tuple[str, str]]:
    """[(heading path, text including its heading line)]"""
    sections: list[tuple[str, list[str]]] = [("", [])]
    stack: list[tuple[int, str]] = []
    in_fence = False
    for line in md.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            sections.append((" > ".join(t for _, t in stack), [line]))
        else:
            sections[-1][1].append(line)
    return [(path, "\n".join(lines).strip()) for path, lines in sections if "\n".join(lines).strip()]


def chunk_markdown(md: str, size: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    buf, buf_path = "", ""

    def flush():
        nonlocal buf, buf_path
        if buf.strip():
            chunks.append(Chunk(buf.strip(), buf_path))
        buf, buf_path = "", ""

    for path, text in _sections(md):
        if len(text) > size:
            flush()
            carry = ""  # a heading line must stay attached to the text after it
            for part in (p.strip() for p in _split(text, size, _SEPS)):
                if not part:
                    continue
                if carry:
                    part, carry = f"{carry}\n\n{part}", ""
                if _HEADING.match(part) and "\n" not in part:
                    carry = part
                    continue
                chunks.append(Chunk(part, path))
            if carry:
                chunks.append(Chunk(carry, path))
            continue
        if buf and len(buf) + 2 + len(text) > size:
            flush()
        if not buf:
            buf_path = path
        buf = f"{buf}\n\n{text}" if buf else text
    flush()
    return chunks
