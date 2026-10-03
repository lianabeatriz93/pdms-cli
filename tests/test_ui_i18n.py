"""pdms ui texts: every text of the page has its Spanish translation in static/i18n.js, and nothing more."""

from __future__ import annotations

import json
import re
from html import escape
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).parents[1] / "src" / "pdms_cli" / "ui" / "static"
PHRASE_TAGS = {"code", "b", "i", "em", "strong", "kbd", "br"}
VOID = {"br", "img", "input", "link", "meta", "hr"}
SKIPPED = {"script", "style", "textarea", "title", "head"}
ATTRS = ("placeholder", "title", "aria-label")
PLACEHOLDER = re.compile(r"\{(\w+)\}")


class Node:
    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag, self.attrs, self.children = tag, attrs, []

    def elements(self) -> list[Node]:
        return [child for child in self.children if isinstance(child, Node)]

    def html(self) -> str:
        """innerHTML as a browser serializes it (only used for phrases: inline tags without attributes)."""
        out = []
        for child in self.children:
            if isinstance(child, str):
                out.append(escape(child, quote=False))
            elif child.tag in VOID:
                out.append(f"<{child.tag}>")
            else:
                out.append(f"<{child.tag}>{child.html()}</{child.tag}>")
        return "".join(out)


class Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag not in VOID and tag in [node.tag for node in self.stack]:
            while self.stack.pop().tag != tag:
                pass

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_phrase(node: Node) -> bool:
    elements = node.elements()
    return bool(elements) and all(e.tag in PHRASE_TAGS and not e.attrs and not e.elements() for e in elements) and any(
        isinstance(child, str) and child.strip() for child in node.children
    )


def html_texts(node: Node, out: list[str]) -> list[str]:
    """What i18n.js collectStatics() translates: the same walk over index.html."""
    if node.tag in SKIPPED or node.attrs.get("translate") == "no":
        return out
    out.extend(node.attrs[attr] for attr in ATTRS if node.attrs.get(attr, "").strip())
    if is_phrase(node):
        out.append(squash(node.html()))
        return out
    for child in node.children:
        if isinstance(child, str):
            if child.strip():
                out.append(squash(child))
        else:
            html_texts(child, out)
    return out


def page_texts() -> set[str]:
    tree = Tree()
    tree.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    return set(html_texts(tree.root, []))


JS_STRING = r'"((?:\\.|[^"\\])*)"'


def page_scripts() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted((STATIC / "js").glob("*.js")))


def js_texts() -> set[str]:
    source = page_scripts()
    return {json.loads(f'"{text}"') for text in re.findall(r"\b(?:t|N_)\(\s*" + JS_STRING, source)}


def catalog() -> tuple[dict[str, str], set[str]]:
    source = (STATIC / "i18n.js").read_text(encoding="utf-8")
    body = source[source.index("const CATALOG = {"):]
    body = body[body.index("es: {"): body.index("\n};")]
    pairs = re.findall(JS_STRING + r"\s*:\s*" + JS_STRING, body)
    keep = re.search(r"const KEEP = new Set\(\[(.*?)\]\);", source, re.S).group(1)
    return ({json.loads(f'"{k}"'): json.loads(f'"{v}"') for k, v in pairs},
            {json.loads(f'"{k}"') for k in re.findall(JS_STRING, keep)})


def test_every_text_has_a_spanish_translation() -> None:
    es, keep = catalog()
    missing = sorted((page_texts() | js_texts()) - set(es) - keep)
    assert not missing, "Missing in i18n.js CATALOG.es:\n" + "\n".join(missing)


def test_the_catalog_has_no_unused_entries() -> None:
    es, keep = catalog()
    used = page_texts() | js_texts()
    assert not sorted(set(es) - used), sorted(set(es) - used)
    assert not sorted(keep - used), sorted(keep - used)


def test_placeholders_and_markup_match() -> None:
    es, _keep = catalog()
    for english, spanish in es.items():
        assert sorted(PLACEHOLDER.findall(english)) == sorted(PLACEHOLDER.findall(spanish)), english
        assert re.findall(r"</?\w+>", english) == re.findall(r"</?\w+>", spanish) or sorted(
            re.findall(r"</?\w+>", english)) == sorted(re.findall(r"</?\w+>", spanish)), english


def test_the_scripts_never_pass_a_template_to_t() -> None:
    source = page_scripts()
    assert not re.findall(r"\b(?:t|N_)\(\s*`", source)
