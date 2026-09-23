"""Offline display of explicit LaTeX math; canonical text is never rewritten.

Derived from proof-graphify scripts/overview_math.py v1.0.0 (MIT). This is
a presentation adapter, not a TeX interpreter. Unsupported expressions remain
inspectable as labeled literal text. latex2mathml is optional at import time, so
an unavailable converter cannot hide evidence.
"""
from __future__ import annotations

import html
import re
from functools import lru_cache
from xml.etree import ElementTree as ET


MATHML_NS = "http://www.w3.org/1998/Math/MathML"
_ENVIRONMENTS = frozenset((
    "cases matrix matrix* pmatrix pmatrix* bmatrix bmatrix* Bmatrix Bmatrix* "
    "vmatrix vmatrix* Vmatrix Vmatrix* smallmatrix array split align align* "
    "substack displaylines eqalign eqalignno"
).split())
CONFIGURATION = {
    "adapter_version": 2,
    "delimiters": [["$", "$"], ["$$", "$$"], [r"\(", r"\)"], [r"\[", r"\]"]],
    "output": "static native MathML with original LaTeX annotation",
    "maximum_formula_characters": 8192,
    "macro_expansion": False,
    "supported_environments": sorted(_ENVIRONMENTS),
}
_TAGS = frozenset((
    "math mrow mi mn mo mtext mspace ms mfrac msqrt mroot mstyle "
    "mpadded mphantom mfenced menclose msub msup msubsup munder mover "
    "munderover mmultiscripts mprescripts none mtable mtr mtd mlabeledtr"
).split())
_ATTRIBUTES = frozenset((
    "display displaystyle scriptlevel mathvariant stretchy fence separator form linebreak "
    "lspace rspace minsize maxsize movablelimits accent accentunder linethickness "
    "bevelled numalign denomalign rowalign columnalign groupalign align rowspacing "
    "columnspacing columnwidth width height depth voffset notation open close "
    "separators equalrows equalcolumns columnlines rowlines frame framespacing "
    "side minlabelspacing rowspan columnspan"
).split())


def _escaped(text: str, index: int) -> bool:
    start = index
    while start > 0 and text[start - 1] == "\\":
        start -= 1
    return (index - start) % 2 == 1


def _spans(text: str):
    """Yield explicit math spans; a final unmatched opener retains its full tail."""
    index = 0
    while index < len(text):
        if _escaped(text, index):
            index += 1
            continue
        if text.startswith("$$", index):
            opening, closing, display = "$$", "$$", "block"
        elif text[index] == "$":
            opening, closing, display = "$", "$", "inline"
        elif text.startswith(r"\(", index):
            opening, closing, display = r"\(", r"\)", "inline"
        elif text.startswith(r"\[", index):
            opening, closing, display = r"\[", r"\]", "block"
        else:
            index += 1
            continue
        end = index + len(opening)
        while end < len(text):
            if text.startswith(closing, end) and not _escaped(text, end):
                if closing != "$" or not (text.startswith("$$", end) or text[end - 1] == "$"):
                    break
            end += 1
        if end == len(text):
            yield index, len(text), None, display
            return
        finish = end + len(closing)
        yield index, finish, text[index + len(opening):end], display
        index = finish


def _check_tex(tex: str) -> None:
    """Reject structures the converter can otherwise silently discard or expand."""
    depth = 0
    for index, character in enumerate(tex):
        if character in "{}" and not _escaped(tex, index):
            depth += 1 if character == "{" else -1
            if depth < 0:
                raise ValueError("unmatched brace")
    if depth:
        raise ValueError("unmatched brace")
    for match in re.finditer(r"\\(?:begin|end)\s*\{([^{}]*)\}", tex):
        if not _escaped(tex, match.start()) and match[1] not in _ENVIRONMENTS:
            raise ValueError("unsupported environment")
    for match in re.finditer(r"\\(?:newcommand|renewcommand|providecommand|def|gdef|edef|xdef|let|DeclareMathOperator|newenvironment|renewenvironment)\b", tex):
        if not _escaped(tex, match.start()):
            raise ValueError("source macro definitions are not expanded")


def _safe_mathml(value: str, tex: str, display: str) -> str:
    if len(value) > 250_000 or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", value, re.I):
        raise ValueError("unsupported converter output")
    root = ET.fromstring(value)
    elements = list(root.iter())
    if len(elements) > 10_000:
        raise ValueError("formula is too large")
    for element in elements:
        tag = element.tag
        if tag.startswith("{" + MATHML_NS + "}"):
            tag = tag[len(MATHML_NS) + 2:]
        if tag not in _TAGS or any(name not in _ATTRIBUTES for name in element.attrib):
            raise ValueError("unsupported converter markup")
        if re.search(r"\\[A-Za-z]+", (element.text or "") + (element.tail or "")):
            raise ValueError("an unsupported LaTeX command remains literal")
        arity = {"mfrac": 2, "mroot": 2, "msub": 2, "msup": 2,
                 "munder": 2, "mover": 2, "msubsup": 3, "munderover": 3}.get(tag)
        if arity is not None and len(element) != arity:
            raise ValueError("incomplete mathematical structure")
        element.tag = tag
    if root.tag != "math":
        raise ValueError("converter did not return MathML")
    root.set("xmlns", MATHML_NS)
    root.set("display", display)
    root.set("aria-label", "LaTeX: " + tex)
    root.set("class", "math-display" if display == "block" else "math-inline")
    contents = list(root)
    root[:] = []
    semantics = ET.SubElement(root, "semantics")
    row = ET.SubElement(semantics, "mrow")
    row.extend(contents)
    ET.SubElement(semantics, "annotation", {"encoding": "application/x-tex"}).text = tex
    return ET.tostring(root, encoding="unicode", short_empty_elements=False)


def _group_scripted_binomials(tex: str) -> str:
    """Group only the converter input; keep the original TeX as evidence.

    latex2mathml 3.81.0 emits a binomial's two fences and fraction as three
    children of a script node. Explicit TeX grouping gives that node one base.
    Limit the workaround to binomials with two braced arguments and a script.
    """
    insertions = []
    for match in re.finditer(r"\\binom\b", tex):
        if _escaped(tex, match.start()):
            continue
        end = match.end()
        for _ in range(2):
            while end < len(tex) and tex[end].isspace():
                end += 1
            if end == len(tex) or tex[end] != "{":
                break
            depth = 1
            end += 1
            while end < len(tex) and depth:
                if tex[end] in "{}" and not _escaped(tex, end):
                    depth += 1 if tex[end] == "{" else -1
                end += 1
            if depth:
                break
        else:
            script = end
            while script < len(tex) and tex[script].isspace():
                script += 1
            if script < len(tex) and tex[script] in "^_":
                insertions.extend(((match.start(), "{"), (end, "}")))
    for index, brace in sorted(insertions, reverse=True):
        tex = tex[:index] + brace + tex[index:]
    return tex


@lru_cache(maxsize=512)
def _convert(tex: str, display: str) -> tuple[str | None, str]:
    if not tex.strip() or len(tex) > CONFIGURATION["maximum_formula_characters"]:
        return None, "The expression is empty or exceeds the display limit."
    try:
        from latex2mathml.converter import convert
    except ImportError:
        return None, "The shared LaTeX converter is unavailable."
    try:
        _check_tex(tex)
        return _safe_mathml(convert(_group_scripted_binomials(tex), display=display), tex, display), ""
    except Exception:
        # Converter failures must never remove the original mathematical text.
        return None, "This LaTeX expression is unsupported by the offline converter."


def _fallback(literal: str, reason: str) -> str:
    return ('<span class="math-fallback" title="' + html.escape(reason, quote=True)
            + '"><span class="math-fallback-label">LaTeX (not rendered): </span><code>'
            + html.escape(literal, quote=True) + '</code></span>')


def render_text(text: str) -> str:
    """Escape prose and typeset only explicit math, without changing source text."""
    text = str(text)
    parts, previous = [], 0
    for start, end, tex, display in _spans(text):
        parts.append(html.escape(text[previous:start], quote=True))
        if tex is None:
            markup, reason = None, "The LaTeX opening delimiter has no matching closing delimiter."
        else:
            markup, reason = _convert(tex, display)
        parts.append(markup if markup is not None else _fallback(text[start:end], reason))
        previous = end
    parts.append(html.escape(text[previous:], quote=True))
    return "".join(parts)
