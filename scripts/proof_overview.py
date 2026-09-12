"""Validate a paper's major-item dataset and render it through the Archify viewer.

This checks record integrity and source anchors, never mathematical validity.
Only shared Python, Node.js and latex2mathml are used; no per-paper program.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from overview_math import render_text


KINDS = {"assumption", "definition", "lemma", "proposition", "theorem", "corollary", "external_result"}
USE_TYPES = {"dependency", "definition", "proof_argument"}
SCRIPT_DIR = Path(__file__).resolve().parent


class OverviewError(ValueError):
    """An actionable source, record, or rendering error."""


def _fields(value, required, optional, context):
    if not isinstance(value, dict):
        raise OverviewError(f"{context}: expected an object.")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing:
        raise OverviewError(f"{context}: missing {', '.join(sorted(missing))}.")
    if extra:
        raise OverviewError(f"{context}: unknown fields {', '.join(sorted(extra))}; check the dataset format.")


def _text(value, context):
    if not isinstance(value, str) or not value.strip():
        raise OverviewError(f"{context}: provide nonempty text.")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise OverviewError(f"{context}: contains an unexpected control character; check JSON backslash escaping.")
    return value


def _positive(value, context):
    if type(value) is not int or value < 1:
        raise OverviewError(f"{context}: expected a positive integer.")


def _source_path(value, base_dir, context):
    _text(value, context)
    path = (base_dir / value).resolve()
    if not path.is_file():
        raise OverviewError(f"{context}: source file does not exist: {path}. Correct the path or supply the source.")
    return path


def _source(value, root, base_dir, context, cache):
    _fields(value, (), ("file", "start_line", "end_line", "page", "label"), context)
    if not any(k in value for k in ("label", "page", "start_line")):
        raise OverviewError(f"{context}: provide a manuscript label, reviewed PDF page, or exact line range.")
    display = []
    if "label" in value:
        display.append(_text(value["label"], context + ".label"))
    if "page" in value:
        _positive(value["page"], context + ".page")
        display.append(f"PDF p. {value['page']}")
    has_start, has_end = "start_line" in value, "end_line" in value
    if has_start != has_end:
        raise OverviewError(f"{context}: start_line and end_line must be supplied together.")
    file = value.get("file", root.get("file"))
    path = _source_path(file, base_dir, context + ".file") if file is not None else None
    excerpt = None
    if path:
        display.append(path.name)
    if has_start:
        start, end = value["start_line"], value["end_line"]
        _positive(start, context + ".start_line")
        _positive(end, context + ".end_line")
        if start > end:
            raise OverviewError(f"{context}: line range starts after it ends.")
        if not path:
            raise OverviewError(f"{context}: a line range needs a source file.")
        if path.suffix.lower() == ".pdf":
            raise OverviewError(f"{context}: use page and label for PDF sources; line ranges require a text source.")
        if path not in cache:
            try:
                cache[path] = path.read_text(encoding="utf-8-sig").splitlines()
            except (OSError, UnicodeError) as exc:
                raise OverviewError(f"{context}: cannot read source as UTF-8 text: {path}.") from exc
        lines = cache[path]
        if end > len(lines):
            raise OverviewError(f"{context}: line range {start}-{end} exceeds the source's {len(lines)} lines.")
        excerpt = "\n".join(lines[start - 1:end])
        display.append(f"lines {start}-{end}")
    result = {"source_display": " · ".join(display)}
    if excerpt is not None:
        result["source_excerpt"] = excerpt
    return result


def validate_data(data: dict, base_dir: Path) -> dict:
    """Return renderer-ready data without changing the authored input."""
    base_dir = Path(base_dir).resolve()
    _fields(data, ("schema_version", "title", "scope", "source", "items", "uses"), ("main_items",), "Dataset")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise OverviewError("Dataset: supported schema_version is 1.")
    for field in ("title", "scope"):
        _text(data[field], field)
    root = data["source"]
    _fields(root, ("title",), ("file",), "Paper source")
    _text(root["title"], "Paper source.title")
    if "file" in root:
        _source_path(root["file"], base_dir, "Paper source.file")
    if not isinstance(data["items"], list) or not data["items"]:
        raise OverviewError("Dataset: items must be a nonempty list of major mathematical items.")
    if not isinstance(data["uses"], list):
        raise OverviewError("Dataset: uses must be a list, possibly empty.")
    prepared = copy.deepcopy(data)
    warnings, cache, by_id = [], {}, {}
    for index, item in enumerate(prepared["items"], 1):
        context = f"Item {index}"
        _fields(item, ("id", "kind", "label", "caption", "statement", "source"), ("uncertainty",), context)
        for field in ("id", "kind", "label", "caption", "statement"):
            _text(item[field], context + "." + field)
        context = item["label"]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", item["id"]):
            raise OverviewError(f"{context}: id must start with a letter and use only letters, digits, hyphens or underscores.")
        if item["id"] in by_id:
            raise OverviewError(f"{context}: duplicate item id {item['id']!r}.")
        if item["kind"] not in KINDS:
            raise OverviewError(f"{context}: unsupported kind {item['kind']!r}; use a major mathematical item type.")
        if "uncertainty" in item:
            _text(item["uncertainty"], context + ".uncertainty")
        item.update(_source(item["source"], root, base_dir, context + " source", cache))
        item["statement_html"] = render_text(item["statement"])
        if 'class="math-fallback"' in item["statement_html"]:
            warnings.append(f"{context}: some LaTeX could not be typeset; the original expression remains visibly labeled.")
        by_id[item["id"]] = item
    if "main_items" in prepared:
        if not isinstance(prepared["main_items"], list) or not prepared["main_items"]:
            raise OverviewError("Dataset.main_items: expected a nonempty list of existing item IDs; omit main_items to use automatic terminal-result selection.")
        selected = set()
        for index, item_id in enumerate(prepared["main_items"], 1):
            _text(item_id, f"Dataset.main_items entry {index}")
            if item_id not in by_id:
                raise OverviewError(f"Dataset.main_items: references unknown item {item_id!r}.")
            if item_id in selected:
                raise OverviewError(f"Dataset.main_items: duplicate item id {item_id!r}; select each main result once.")
            selected.add(item_id)
    pairs, outgoing, incoming = set(), {key: [] for key in by_id}, {key: 0 for key in by_id}
    for index, use in enumerate(prepared["uses"], 1):
        context = f"Use {index}"
        _fields(use, ("from", "to", "reason"), ("source", "uncertainty", "type", "regime"), context)
        for field in ("from", "to", "reason"):
            _text(use[field], context + "." + field)
        for endpoint in ("from", "to"):
            if use[endpoint] not in by_id:
                raise OverviewError(f"{context}: {endpoint} references unknown item {use[endpoint]!r}.")
        start, end = use["from"], use["to"]
        context = f"{by_id[start]['label']} to {by_id[end]['label']}"
        use.setdefault("type", "dependency")
        _text(use["type"], context + ".type")
        if use["type"] not in USE_TYPES:
            raise OverviewError(f"{context}: unsupported use type {use['type']!r}; use dependency, definition, or proof_argument.")
        if "regime" in use:
            _text(use["regime"], context + ".regime")
        if start == end:
            raise OverviewError(f"{context}: a self dependency is not supported. Inspect the source or separate the actual items.")
        if (start, end) in pairs:
            raise OverviewError(f"{context}: duplicate use. Combine contributions in one reason.")
        pairs.add((start, end))
        outgoing[start].append(end)
        incoming[end] += 1
        if "uncertainty" in use:
            _text(use["uncertainty"], context + ".uncertainty")
        if "source" in use:
            use.update(_source(use["source"], root, base_dir, context + " source", cache))
        use["id"] = "use-" + hashlib.sha256((start + "\0" + end).encode()).hexdigest()[:16]
        use["reason_html"] = render_text(use["reason"])
        if 'class="math-fallback"' in use["reason_html"]:
            warnings.append(f"{context}: a dependency reason contains LaTeX that could not be typeset.")
    # Kahn traversal catches circular input without changing its meaning or hiding links.
    ready = [key for key, count in incoming.items() if count == 0]
    for key in ready:
        for end in outgoing[key]:
            incoming[end] -= 1
            if incoming[end] == 0:
                ready.append(end)
    if len(ready) != len(by_id):
        blocked = ", ".join(by_id[key]["label"] for key, count in incoming.items() if count)
        raise OverviewError(f"Dependency cycle blocks the layout, including: {blocked}. Inspect the directions and source argument. This overview renderer needs an acyclic map; do not delete a real circularity merely to pass validation.")
    prepared["warnings"] = warnings
    return prepared


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise OverviewError(f"JSON contains duplicate field {key!r}; retain one intended value.")
        value[key] = item
    return value


def load_data(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OverviewError(f"Cannot read dataset {path}: {exc}") from exc


def render_file(input_path: Path, output_path: Path) -> dict:
    started = time.perf_counter()
    input_path, output_path = Path(input_path).resolve(), Path(output_path).resolve()
    data = load_data(input_path)
    prepared = validate_data(data, input_path.parent)
    protected = {input_path}
    for source in [data["source"]] + [i["source"] for i in data["items"]] + [u["source"] for u in data["uses"] if "source" in u]:
        if "file" in source:
            protected.add((input_path.parent / source["file"]).resolve())
    if output_path in protected or output_path.suffix.lower() != ".html":
        raise OverviewError("Choose an .html output path distinct from the dataset and manuscript files.")
    node = shutil.which("node")
    if not node:
        raise OverviewError("Shared Node.js is unavailable on PATH. Install Node.js globally or expose your existing shared installation; do not create a project-local runtime.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="proof-overview-", dir=output_path.parent) as temp:
            staged_input, staged_output = Path(temp) / "prepared.json", Path(temp) / "overview.html"
            staged_input.write_text(json.dumps(prepared, ensure_ascii=False), encoding="utf-8")
            run = subprocess.run([node, str(SCRIPT_DIR / "render.mjs"), str(staged_input), str(staged_output)], capture_output=True, text=True, encoding="utf-8", timeout=60)
            if run.returncode:
                raise OverviewError("Viewer rendering failed: " + (run.stderr or run.stdout).strip()[:2000])
            if not staged_output.is_file() or "<svg" not in staged_output.read_text(encoding="utf-8"):
                raise OverviewError("Viewer did not produce an SVG HTML overview; the existing output was preserved.")
            staged_output.replace(output_path)
    except subprocess.TimeoutExpired as exc:
        raise OverviewError("Viewer rendering exceeded 60 seconds; the existing output was preserved.") from exc
    except OSError as exc:
        raise OverviewError(f"Could not write the overview: {exc}") from exc
    return {"output": str(output_path), "items": len(data["items"]), "uses": len(data["uses"]),
            "bytes": output_path.stat().st_size, "seconds": round(time.perf_counter() - started, 3),
            "sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(), "warnings": prepared["warnings"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("validate", help="Check dataset integrity and source anchors")
    check.add_argument("dataset", type=Path)
    render = commands.add_parser("render", help="Render the validated dataset as standalone HTML")
    render.add_argument("dataset", type=Path)
    render.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            data = validate_data(load_data(args.dataset), args.dataset.resolve().parent)
            result = {"valid": True, "items": len(data["items"]), "uses": len(data["uses"]), "warnings": data["warnings"]}
        else:
            result = render_file(args.dataset, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except OverviewError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
