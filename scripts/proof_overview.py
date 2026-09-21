"""Validate a paper's major-item dataset and render it through the Archify viewer.

This checks record integrity and source anchors, never mathematical validity.
Only shared Python, Node.js and latex2mathml are used; no per-paper program.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from html.parser import HTMLParser

from paper_records import RecordError


SCRIPT_DIR = Path(__file__).resolve().parent

OverviewError = RecordError


def validate_data(data: dict, base_dir: Path) -> dict:
    """Validate native records and prepare their renderer projection."""
    from paper_records import normalize, _prepare_records_validated
    return _prepare_records_validated(normalize(data, base_dir), base_dir)


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


class _ArtifactReader(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nodes, self.uses, self.index_nodes, self.index_uses = [], [], [], []
        self.details, self.detail_uses = [], []
        self.svg_depth = 0
        self.template_depth = 0
        self.current_article = None
        self.metadata_count = 0
        self.in_metadata = False
        self.metadata = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'template':
            self.template_depth += 1
        if tag == 'svg':
            self.svg_depth += 1
        if self.svg_depth and tag == 'g' and 'data-node-id' in attrs:
            self.nodes.append(attrs['data-node-id'])
        if self.svg_depth and tag == 'path' and 'data-edge-id' in attrs:
            self.uses.append((attrs['data-edge-id'], attrs.get('data-edge-from'), attrs.get('data-edge-to')))
        if tag == 'article' and 'data-proof-index-item' in attrs:
            self.index_nodes.append(attrs['data-proof-index-item'])
            self.current_article = attrs['data-proof-index-item']
        if tag == 'article' and 'data-proof-index-use' in attrs:
            self.index_uses.append((attrs['data-proof-index-use'], attrs.get('data-proof-from'), attrs.get('data-proof-to')))
        # Detail markers also appear inside the inert panel templates; only
        # the static index copies are the canonical representation.
        if not self.template_depth:
            if 'data-proof-detail' in attrs:
                self.details.append((attrs['data-proof-detail'], self.current_article))
            if 'data-proof-detail-use' in attrs:
                self.detail_uses.append((attrs['data-proof-detail-use'], attrs.get('data-proof-from'), attrs.get('data-proof-to'), self.current_article))
        if tag == 'script' and attrs.get('id') == 'proof-overview-records':
            self.metadata_count += 1
            self.in_metadata = True

    def handle_endtag(self, tag):
        if tag == 'template':
            self.template_depth = max(0, self.template_depth - 1)
        if tag == 'svg':
            self.svg_depth = max(0, self.svg_depth - 1)
        if tag == 'article':
            self.current_article = None
        if tag == 'script':
            self.in_metadata = False

    def handle_data(self, value):
        if self.in_metadata:
            self.metadata.append(value)


def _accept_artifact(prepared, html, receipt, input_bytes):
    """Inspect the delivered representation independently of renderer counts."""
    if receipt.get('input_sha256') != hashlib.sha256(input_bytes).hexdigest():
        raise OverviewError('Viewer receipt input hash does not match the checked snapshot; the previous output was preserved.')
    if receipt.get('artifact_sha256') != hashlib.sha256(html).hexdigest():
        raise OverviewError('Viewer receipt artifact hash does not match the candidate HTML; the previous output was preserved.')
    parser = _ArtifactReader()
    try:
        parser.feed(html.decode('utf-8'))
        metadata = json.loads(''.join(parser.metadata), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as exc:
        raise OverviewError('Viewer artifact has missing or invalid record metadata; the previous output was preserved.') from exc
    if parser.metadata_count != 1 or not isinstance(metadata, dict):
        raise OverviewError('Viewer artifact must contain exactly one canonical record projection; the previous output was preserved.')
    for key in ('items', 'uses', 'details', 'detail_uses', 'build_context', 'graph_mode', 'graph_cycles'):
        if metadata.get(key) != prepared.get(key):
            raise OverviewError(f'Viewer artifact changed the prepared {key}; the previous output was preserved.')
    expected_nodes = sorted(item['id'] for item in prepared['items'])
    expected_uses = sorted((use['id'], use['from'], use['to']) for use in prepared['uses'])
    if sorted(parser.index_nodes) != expected_nodes or sorted(parser.index_uses) != expected_uses:
        raise OverviewError('Viewer statement index omitted, duplicated, or substituted an item or use; the previous output was preserved.')
    if prepared['graph_mode'] != 'index' and (sorted(parser.nodes) != expected_nodes or sorted(parser.uses) != expected_uses):
        raise OverviewError('Viewer graph omitted, duplicated, or substituted an item or use; the previous output was preserved.')
    expected_details = sorted(row['id'] for row in prepared['details'])
    expected_detail_uses = sorted((use['id'], use['from'], use['to']) for use in prepared['detail_uses'])
    if (sorted(detail for detail, _ in parser.details) != expected_details
            or sorted((use, start, end) for use, start, end, _ in parser.detail_uses) != expected_detail_uses):
        raise OverviewError('Viewer omitted, duplicated, or substituted an intermediate row or detail use; the previous output was preserved.')
    owners = {row['id']: row['owner'] for row in prepared['details']}
    for detail, article in parser.details:
        if article != owners[detail]:
            raise OverviewError('Viewer placed an intermediate row outside its owner; the previous output was preserved.')
    for use, start, end, article in parser.detail_uses:
        endpoint = start if start in owners else end
        if article != owners[endpoint]:
            raise OverviewError('Viewer placed a detail use outside its owner context; the previous output was preserved.')
    expected_geometry = 'not_applicable' if prepared['graph_mode'] == 'index' else 'pass'
    if not isinstance(receipt.get('geometry'), dict) or receipt['geometry'].get('status') != expected_geometry:
        raise OverviewError('Viewer geometry checks did not complete successfully; the previous output was preserved.')
    return {'status': 'pass', 'representation': prepared['graph_mode'],
            'items': len(expected_nodes), 'uses': len(expected_uses),
            'details': len(expected_details), 'detail_uses': len(expected_detail_uses), 'index': 'pass'}


def _renderer_version():
    paths = list(SCRIPT_DIR.glob('*.py')) + [SCRIPT_DIR / 'render.mjs'] + list((SCRIPT_DIR.parent / 'assets' / 'archify').glob('*'))
    identity = hashlib.sha256()
    for path in sorted(p for p in paths if p.is_file()):
        identity.update(path.relative_to(SCRIPT_DIR.parent).as_posix().encode())
        identity.update(b'\0')
        identity.update(path.read_bytes())
    return identity.hexdigest()


def render_dataset(data: dict, base_dir: Path, output_path: Path, protected_paths=()) -> dict:
    """Render one immutable exported snapshot, independent of its storage backend."""
    from paper_records import normalize
    started = time.perf_counter()
    canonical = normalize(data, base_dir)
    return _render_validated_dataset(canonical, base_dir, output_path, protected_paths, started)


def _render_validated_dataset(canonical, base_dir, output_path, protected_paths=(), started=None):
    """Render input validated by the file or database entry point, without edits."""
    from paper_records import _prepare_records_validated, source_status
    started = time.perf_counter() if started is None else started
    base_dir, output_path = Path(base_dir).resolve(), Path(output_path).resolve()
    prepared = _prepare_records_validated(canonical, base_dir)
    version = _renderer_version()
    prepared['build_context']['renderer_version'] = version
    protected = {Path(p).resolve() for p in protected_paths}
    protected.update((base_dir / f['path']).resolve() for f in canonical['source_revision']['files'])
    if output_path in protected or output_path.suffix.lower() != ".html":
        raise OverviewError("Choose an .html output path distinct from the dataset and manuscript files.")
    node = shutil.which("node")
    if not node:
        raise OverviewError("Shared Node.js is unavailable on PATH. Install Node.js globally or expose your existing shared installation; do not create a project-local runtime.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="proof-overview-", dir=output_path.parent) as temp:
            staged_input, staged_output = Path(temp) / "prepared.json", Path(temp) / "overview.html"
            input_bytes = json.dumps(prepared, ensure_ascii=False, allow_nan=False).encode('utf-8')
            staged_input.write_bytes(input_bytes)
            run = subprocess.run([node, str(SCRIPT_DIR / "render.mjs"), str(staged_input), str(staged_output)], capture_output=True, text=True, encoding="utf-8", timeout=60)
            if run.returncode:
                raise OverviewError("Viewer rendering failed: " + (run.stderr or run.stdout).strip()[:2000])
            if not staged_output.is_file():
                raise OverviewError("Viewer did not produce an HTML overview; the existing output was preserved.")
            try:
                renderer_receipt = json.loads(run.stdout, object_pairs_hook=_unique_object)
            except (ValueError, TypeError) as exc:
                raise OverviewError('Viewer did not return a valid delivery receipt; the existing output was preserved.') from exc
            if not isinstance(renderer_receipt, dict):
                raise OverviewError('Viewer receipt is not an object; the existing output was preserved.')
            html = staged_output.read_bytes()
            preservation = _accept_artifact(prepared, html, renderer_receipt, input_bytes)
            if version != _renderer_version():
                raise OverviewError('Renderer files changed during generation; retry using one renderer version. The existing output was preserved.')
            if source_status(canonical, base_dir) != prepared['build_context']['source_status']:
                raise OverviewError('The manuscript changed during generation. Retry to report its source freshness accurately; the existing output was preserved.')
            staged_output.replace(output_path)
    except subprocess.TimeoutExpired as exc:
        raise OverviewError("Viewer rendering exceeded 60 seconds; the existing output was preserved.") from exc
    except OSError as exc:
        raise OverviewError(f"Could not write the overview: {exc}") from exc
    return {"output": str(output_path), "items": len(canonical["items"]), "uses": len(canonical["uses"]),
            "bytes": output_path.stat().st_size, "seconds": round(time.perf_counter() - started, 3),
            "sha256": hashlib.sha256(html).hexdigest(), "warnings": prepared["warnings"],
            "math_diagnostics": prepared.get("math_diagnostics", []),
            **prepared['build_context'], 'graph_preservation': preservation,
            'geometry': renderer_receipt['geometry'], 'graph_mode': prepared['graph_mode'],
            'graph_cycles': prepared['graph_cycles'],
            'browser_review': 'not_performed', 'visual_review': 'not_performed'}


def render_file(input_path: Path, output_path: Path) -> dict:
    input_path = Path(input_path).resolve()
    return render_dataset(load_data(input_path), input_path.parent, output_path, protected_paths=(input_path,))


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
            from paper_records import normalize, record_report
            base = args.dataset.resolve().parent
            data = normalize(load_data(args.dataset), base)
            report = record_report(data, base)
            result = {"valid": True, "items": len(data["items"]), "uses": len(data["uses"]), "warnings": report["warnings"],
                      'graph_mode': report['graph_mode'], 'graph_cycles': report['graph_cycles'], **report['build_context']}
        else:
            result = render_file(args.dataset, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except OverviewError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
