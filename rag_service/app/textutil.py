"""Cleaning, tokenising, keyword (sparse) vectors, language-script detection, Shopify URL metadata."""
import re
import unicodedata
import zlib
from collections import Counter
from urllib.parse import urlparse

_ZERO_WIDTH = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def clean_markdown(text: str) -> str:
    """Drops images, link URLs, HTML comments and invisible characters (a common place to hide
    prompt-injection text), and tidies whitespace. Keeps headings and paragraphs."""
    text = _HTML_COMMENT.sub("", text or "")
    text = _ZERO_WIDTH.sub("", text)
    text = _IMG.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------- keyword (sparse) vectors
def tokenize(text: str) -> list[str]:
    """Unicode-aware: keeps combining marks, so Hindi / Punjabi words (matras) aren't split apart."""
    toks, cur = [], []
    for ch in (text or "").lower():
        if ch.isalnum() or unicodedata.category(ch).startswith("M"):
            cur.append(ch)
        elif cur:
            toks.append("".join(cur))
            cur = []
    if cur:
        toks.append("".join(cur))
    out = []
    for t in toks:
        if len(t) < 2 and not t.isdigit():
            continue
        if t.isascii() and len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]  # crude plural folding: "refunds" == "refund"
        out.append(t)
    return out


_K1, _B, _AVGDL = 1.2, 0.75, 150.0


def sparse_vector(text: str, query: bool = False) -> tuple[list[int], list[float]] | None:
    """BM25-style term weights hashed to ids. Qdrant applies IDF server-side (Modifier.IDF), so this
    only supplies the saturated term frequency. Queries use weight 1 per distinct term."""
    toks = tokenize(text)
    if not toks:
        return None
    counts = Counter(toks)
    dl = len(toks)
    weights: dict[int, float] = {}
    for tok, tf in counts.items():
        idx = zlib.crc32(tok.encode("utf-8"))
        w = 1.0 if query else tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * dl / _AVGDL))
        weights[idx] = weights.get(idx, 0.0) + w
    return list(weights), list(weights.values())


# ---------------------------------------------------------------- language script
def detect_script(text: str) -> str | None:
    """'non_latin' when most letters are not Latin (Devanagari, Gurmukhi, Arabic, CJK...). Romanised
    Hindi can't be detected this way: pass hints.language from your classifier."""
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return None
    latin = sum(1 for c in letters if unicodedata.name(c, "").startswith("LATIN"))
    return "non_latin" if (len(letters) - latin) / len(letters) > 0.3 else "latin"


# ---------------------------------------------------------------- Shopify URL / document id -> metadata
_POLICY_WORDS = (
    (("refund", "return"), "refund_policy"),
    (("shipping", "delivery"), "shipping_policy"),
    (("privacy",), "privacy_policy"),
    (("terms",), "terms_of_service"),
    (("warranty",), "warranty_policy"),
)


def infer_metadata(ref: str) -> dict:
    """Guesses kind / policy_type from a URL (/policies/refund-policy, /products/x) or a document id
    ('policy:refund_policy', 'product:linen-tote'). Explicit values given at ingest time win."""
    path = (urlparse(ref).path if "://" in ref else ref).lower()
    kind = None
    if "/policies/" in path or path.startswith("policy:"):
        kind = "policy"
    elif "/products/" in path or path.startswith("product:"):
        kind = "product"
    elif "/collections/" in path:
        kind = "collection"
    elif "/blogs/" in path:
        kind = "blog"
    elif "/pages/" in path:
        kind = "faq" if "faq" in path else "page"
    elif path.startswith("faq:"):
        kind = "faq"
    policy_type = None
    if kind in (None, "policy", "page", "faq"):
        for words, ptype in _POLICY_WORDS:
            if any(w in path for w in words):
                policy_type = ptype
                if kind in (None, "page"):
                    kind = "policy"
                break
    return {"kind": kind, "policy_type": policy_type}
