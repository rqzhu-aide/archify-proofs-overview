"""Offline display of explicit LaTeX math; canonical text is never rewritten.

This is a presentation adapter, not a TeX interpreter. Unsupported expressions
remain inspectable as labeled literal text. The shared latex2mathml package is
optional at import time, so an unavailable converter cannot hide evidence.
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
    "adapter_version": 6,
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
        doubled = False
        if text.startswith(r"\\(", index) or text.startswith(r"\\[", index):
            opening = text[index:index + 3]
            closing = r"\\)" if opening.endswith("(") else r"\\]"
            display = "inline" if opening.endswith("(") else "block"
            doubled = True
        elif text.startswith("$$", index):
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
            # An isolated doubled slash is ordinary prose, often a path. Only
            # a paired math-like delimiter is evidence of accidental escaping.
            if doubled:
                index += len(opening)
                continue
            yield index, len(text), None, display
            return
        finish = end + len(closing)
        yield index, finish, text[index + len(opening):end], display
        index = finish


def _check_tex(tex: str) -> None:
    """Reject structures the converter can otherwise silently discard or expand."""
    # JSON accepts \r, so an under-escaped \rVert becomes a carriage return
    # followed by Vert. The converter then renders the remaining letters as
    # mathematics without an error. Only flag these distinctive remnants:
    # ordinary whitespace and ambiguous newline/tab + words are not evidence
    # of a lost command. A CRLF does not match, nor does a longer identifier.
    damaged = re.search(r"\r(Vert|vert)(?![A-Za-z])", tex)
    if damaged:
        raise ValueError("likely decoded LaTeX escape: carriage return + " + damaged[1]
                         + "; check JSON escaping for \\r" + damaged[1])
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
    # A doubled command slash is accepted by the converter as a line break,
    # then letters (e.g. 'mathbf'), which can silently change the display.
    # Inside a row environment, however, those letters can legitimately begin
    # the next row. Do not infer corruption there.
    environments = []
    for match in re.finditer(
            r"\\(begin|end)\s*\{([^{}]*)\}|(?<!\\)\\\\([A-Za-z]{2,})", tex):
        if _escaped(tex, match.start()):
            continue
        if match[1] == "begin":
            environments.append(match[2])
        elif match[1] == "end":
            if environments and environments[-1] == match[2]:
                environments.pop()
        elif not environments:
            # These braced row commands also permit \\\\ without an environment.
            # Their body is parsed by the converter; avoid guessing whether a
            # following bare word was intended as a command.
            if _in_row_argument(tex, match.start()):
                continue
            raise ValueError("likely doubled LaTeX command slash: " + match.group(0)
                             + "; check the stored text's escaping")


def _in_row_argument(tex: str, position: int) -> bool:
    for match in re.finditer(r"\\(?:substack|displaylines|eqalign|eqalignno)\s*\{", tex):
        if match.end() > position or _escaped(tex, match.start()):
            continue
        depth = 1
        for index in range(match.end(), position):
            if tex[index] in "{}" and not _escaped(tex, index):
                depth += 1 if tex[index] == "{" else -1
            if depth == 0:
                break
        if depth:
            return True
    return False


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
        literal = re.search(r"\\[A-Za-z]+", (element.text or "") + (element.tail or ""))
        if literal:
            raise ValueError(f"an unsupported LaTeX command remains literal: {literal.group(0)}")
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
    Arguments may be braced or single unbraced tokens (\binom{n}{k} or
    \binom nk); anything else is left for the ordinary safety checks.
    """
    def skip_space(index: int) -> int:
        while index < len(tex) and tex[index].isspace():
            index += 1
        return index

    def argument_end(index: int) -> int | None:
        index = skip_space(index)
        if index == len(tex):
            return None
        if tex[index] == "{":
            depth = 1
            index += 1
            while index < len(tex) and depth:
                if tex[index] in "{}" and not _escaped(tex, index):
                    depth += 1 if tex[index] == "{" else -1
                index += 1
            return index if not depth else None
        if tex[index] == "\\":
            token = re.match(r"\\[A-Za-z]+|\\[^A-Za-z]", tex[index:])
            return index + token.end() if token else None
        return index + 1

    insertions = []
    for match in re.finditer(r"\\binom\b", tex):
        if _escaped(tex, match.start()):
            continue
        end = match.end()
        for _ in range(2):
            end = argument_end(end) if end is not None else None
            if end is None:
                break
        else:
            script = skip_space(end)
            if script < len(tex) and tex[script] in "^_":
                insertions.extend(((match.start(), "{"), (end, "}")))
    for index, brace in sorted(insertions, reverse=True):
        tex = tex[:index] + brace + tex[index:]
    return tex


_SIZED_BAR_RE = re.compile(
    r"(\\(?:Bigg[lmr]|bigg[lmr]|Big[lmr]|big[lmr]|Big|big)\s*)"
    r"\\(lVert|rVert|lvert|rvert|Vert|vert)(?![A-Za-z])")


def _sized_named_bars(tex: str) -> str:
    r"""Swap a sized named bar for the symbol form the converter supports.

    latex2mathml 3.81.0 leaves \lvert, \rvert, \vert, \lVert, \rVert, and
    \Vert literal when one directly follows a \big-family sizing command. The
    symbol forms | and \| convert at the same explicit size, so the author's
    sizing is preserved. \left\lVert already converts and is never touched.
    The lookahead is a letter class, not a word boundary: \rVert_{\mathcal H}
    has an underscore after the command name. Converter input only.
    """
    def replace(match: re.Match) -> str:
        if _escaped(tex, match.start()):
            return match.group(0)
        bar = "|" if match.group(2) in ("lvert", "rvert", "vert") else r"\|"
        return match.group(1) + bar

    return _SIZED_BAR_RE.sub(replace, tex)


def _argument_operator(tex: str) -> str:
    r"""Render the actual \arg command as an operator in converter input only."""
    return re.sub(r"\\arg(?![A-Za-z])", lambda match: (
        match.group(0) if _escaped(tex, match.start()) else r"\operatorname{arg}"
    ), tex)


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
        adapted = _argument_operator(_sized_named_bars(_group_scripted_binomials(tex)))
        return _safe_mathml(convert(adapted, display=display), tex, display), ""
    except Exception as exc:
        # Converter failures must never remove the original mathematical text.
        # The bounded reason locates the problem; a traceback would not.
        detail = str(exc).split("\n", 1)[0][:160]
        reason = "This LaTeX expression is unsupported by the offline converter."
        return None, f"{reason} ({detail})" if detail else reason


def _fallback(literal: str, reason: str) -> str:
    return ('<span class="math-fallback" title="' + html.escape(reason, quote=True)
            + '"><span class="math-fallback-label">LaTeX (not rendered): </span><code>'
            + html.escape(literal, quote=True) + '</code></span>')


def render_text(text: str, diagnostics=None) -> str:
    """Escape prose and typeset only explicit math, without changing source text.

    When ``diagnostics`` is a list, each expression that falls back appends one
    entry with a bounded excerpt, the display mode, and the converter's reason,
    so callers can locate the failing record field without custom tooling.
    """
    text = str(text)
    parts, previous = [], 0
    for start, end, tex, display in _spans(text):
        parts.append(html.escape(text[previous:start], quote=True))
        if text.startswith((r"\\(", r"\\["), start):
            markup, reason = None, ("Likely doubled LaTeX delimiter slashes; "
                                    "check the stored text's escaping.")
        elif tex is None:
            markup, reason = None, "The LaTeX opening delimiter has no matching closing delimiter."
        else:
            markup, reason = _convert(tex, display)
        if markup is None and diagnostics is not None:
            diagnostics.append({"excerpt": text[start:end][:200], "display": display, "reason": reason})
        parts.append(markup if markup is not None else _fallback(text[start:end], reason))
        previous = end
    parts.append(html.escape(text[previous:], quote=True))
    return "".join(parts)


def group_diagnostics(diagnostics) -> list[dict]:
    """Summarize repeated display failures without discarding their locations.

    Reasons already identify an unsupported command when available, so one
    correction can be located across several records. The input's full
    per-expression evidence remains untouched.
    """
    groups = {}
    for row in diagnostics:
        reason = row["reason"]
        group = groups.setdefault(reason, {"reason": reason, "count": 0,
                                           "locations": [], "example": row["excerpt"]})
        group["count"] += 1
        location = {key: row[key] for key in ("collection", "id", "field") if key in row}
        if location and location not in group["locations"]:
            group["locations"].append(location)
    return list(groups.values())


def render_scope(text: str, diagnostics=None) -> dict[str, str]:
    """Render a first-paragraph preview and optional full scope disclosure.

    The preview keeps the existing 100-word limit, extending a cutoff to the
    end of any explicit math it crosses. Paragraph breaks inside math are
    ignored. A shortened lead retains the full original in the disclosure;
    otherwise only later paragraphs are disclosed. Repeated preview formulas
    are displayed twice but diagnosed only once, in the complete disclosure.
    """
    original = str(text or "").strip()
    spans = list(_spans(original))
    paragraph = next((match for match in re.finditer(r"\n\s*\n", original)
                      if not any(start < match.end() and end > match.start()
                                 for start, end, _, _ in spans)), None)
    lead = original[:paragraph.start()] if paragraph else original
    remainder = original[paragraph.end():].strip() if paragraph else ""
    words = list(re.finditer(r"\S+", lead))
    if len(words) > 100:
        cutoff = words[100].start()
        for start, end, _, _ in spans:
            if start < cutoff < end:
                cutoff = end
                break
        preview = lead[:cutoff].rstrip()
        if cutoff < len(lead):
            preview += "…"
        return {"lead_html": render_text(preview),
                "details_html": render_text(original, diagnostics=diagnostics)}
    return {"lead_html": render_text(lead.strip(), diagnostics=diagnostics),
            "details_html": render_text(remainder, diagnostics=diagnostics)}
