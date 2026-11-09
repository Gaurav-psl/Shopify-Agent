"""Minimal HTML -> Markdown (stdlib only), for Shopify policy / product bodies. Keeps headings, lists, tables
as text rows, and drops scripts, styles and hidden elements."""
from html.parser import HTMLParser

_SKIP = {"script", "style", "noscript", "template", "svg", "iframe"}
_VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr"}
_BLOCK = {"p", "div", "section", "article", "br", "tr", "table", "ul", "ol", "blockquote", "hr"}


class _P(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        style = (a.get("style") or "").replace(" ", "").lower()
        if tag in _VOID:                       # void tags never get an end tag, so never count them
            if not self.skip and tag in {"br", "hr"}:
                self.out.append("\n")
            return
        if tag in _SKIP or "display:none" in style or "hidden" in a or a.get("aria-hidden") == "true":
            self.skip += 1
            return
        if self.skip:
            self.skip += 1
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in {"td", "th"}:
            self.out.append(" | ")
        elif tag in _BLOCK:
            self.out.append("\n\n" if tag in {"p", "div", "section", "article", "table", "ul", "ol"} else "\n")

    def handle_endtag(self, tag):
        if self.skip:
            self.skip -= 1
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p"}:
            self.out.append("\n\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_markdown(html: str) -> str:
    p = _P()
    p.feed(html or "")
    p.close()
    text = "".join(p.out).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    out, blank = [], 0
    for line in lines:
        blank = blank + 1 if not line else 0
        if blank <= 1:
            out.append(line)
    return "\n".join(out).strip()
