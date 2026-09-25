"""Command line entry for the shared proofcheck core (implementation-handoff 5).

Every command prints exactly one JSON object on stdout; diagnostics go to
stderr. Exit codes: 0 ok, 2 invalid request, 3 conflict, 4 incompatible
database, 5 source unavailable, 6 render or publication failure. An incomplete
mathematical assessment is a successful status query with
``process_complete: false``, not a command failure. Workers never edit the
SQLite file directly; every mutation enters through these commands.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

from . import CONTRACT_NAME, CONTRACT_VERSION, CORE_VERSION, PACKET_VERSION, PROJECTION_VERSION, STORAGE_FORMAT
from .acceptance import apply_batch
from .errors import CoreError, InvalidRequest
from .export_import import import_legacy, migrate_overview, write_export
from .ids import COLLECTIONS, PREFIXES, new_id
from .packets import MODES, get_packet
from .projection import build_projection
from .publish import publish_report
from .queries import changes as query_changes
from .queries import status as query_status
from .queries import validate_snapshot
from .review import compare, map_response, reconcile, record_qualification, submit_review
from .sources import anchor_sources, capture_sources, review_sources
from .storage import Database, initialize, paper_record
from .telemetry import STAGES, events, record_event, summarize

ID_KINDS = tuple(PREFIXES)
PROG = "paper_audit.py"


class _Parser(argparse.ArgumentParser):
    """argparse that reports usage problems as InvalidRequest (exit 2 with a JSON body)."""

    def error(self, message):
        raise InvalidRequest(f"{self.prog}: {message}", code="USAGE")


# -- helpers ----------------------------------------------------------------------
def _load_json(path, *, what="JSON file"):
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise InvalidRequest(f"cannot read {what} {p}: {exc}", code="FILE_UNREADABLE") from exc
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidRequest(f"{what} {p} is not valid UTF-8 JSON: {exc}", code="JSON_INVALID") from exc


def _write_json(path, payload) -> dict:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False)
    staged = p.with_name(p.name + f".tmp-{os.getpid()}")
    staged.write_text(text, encoding="utf-8", newline="\n")
    os.replace(staged, p)
    return {"path": str(p), "bytes": len(text.encode("utf-8"))}


def _write_template(path, payload):
    """Authoring assistance never overwrites an existing response or source."""
    data = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        if not destination.is_file() or destination.read_bytes() != data:
            raise InvalidRequest("template output exists with different content; choose a new output path",
                                 code="OUTPUT_CONFLICT") from None
    return {"path": str(destination), "bytes": len(data)}


def _protect_template_destination(db, path):
    destination = Path(path).resolve()
    protected = {db.path.resolve()}
    for source in db.heads("sources"):
        paper = db.head("papers", source.body["paper_id"])
        if paper is not None:
            protected.add((Path(paper.body["source_root"]) / source.body["path"]).resolve())
    if destination in protected:
        raise InvalidRequest("template output cannot replace a database or registered source, even if absent",
                             code="OUTPUT_CONFLICT")


def cmd_template(args):
    from .assistance import authoring_template
    with Database(args.db) as db:
        _protect_template_destination(db, args.out)
        if db.packet(args.packet) is None:
            raise InvalidRequest(f"unknown packet {args.packet}", code="PACKET_UNKNOWN")
        result = authoring_template(args.collection, packet_id=args.packet, record_id=args.id)
    return {"command": "template", "file": _write_template(args.out, result),
            "next_action": "author the template's scientific fields; submit only its template member"}


def cmd_review_mapping_template(args):
    from .assistance import mapping_template
    from .contract import WORKER_RESPONSE, validate_shape
    from .acceptance import COMMAND_MODES
    from .packets import load_packet
    from .review import _mapped_indexes, classify_judgments
    with Database(args.db) as db:
        _protect_template_destination(db, args.out)
        response = db.head("responses", args.response)
        if response is None or response.retired:
            raise InvalidRequest("mapping template needs a live saved response", code="RESPONSE_UNKNOWN")
        packet = db.packet(args.packet)
        if packet is None:
            raise InvalidRequest(f"unknown packet {args.packet}", code="PACKET_UNKNOWN")
        if packet["manifest"]["mode"] not in COMMAND_MODES["review_map"]:
            raise InvalidRequest("choose a private coordinator mapping packet containing canonical targets",
                                 code="PACKET_MODE")
        try:
            worker = json.loads(db.get_blob(response.body["original_blob"]))
        except (TypeError, ValueError, UnicodeError):
            raise InvalidRequest("unreadable worker response requires a new response, not mapping",
                                 code="RESPONSE_UNREADABLE") from None
        if validate_shape(WORKER_RESPONSE, worker):
            raise InvalidRequest("malformed worker response requires a new response, not mapping",
                                 code="RESPONSE_UNREADABLE")
        original = load_packet(db, response.body["packet_id"])
        _, pending = classify_judgments(original["_manifest"], worker, original)
        already_mapped = _mapped_indexes(db, response.id)
        indexes = args.judgment if args.judgment is not None else [
            i for i, _ in pending if i not in already_mapped]
        if any(i < 0 or i >= len(worker["judgments"]) for i in indexes):
            raise InvalidRequest("judgment indexes must identify saved response rows")
        result = mapping_template(source_packet_id=response.body["packet_id"], mapping_packet_id=args.packet,
                                  response_id=args.response, judgment_indexes=indexes)
    return {"command": "review mapping-template", "file": _write_template(args.out, result),
            "next_action": "author exact target and rationale for each selected row; keep the worker bytes unchanged"}


def _parse_target(text: str) -> dict:
    collection, sep, id = text.partition(":")
    if not sep or not collection or not id:
        raise InvalidRequest(f"targets are written COLLECTION:ID, got {text!r}", code="USAGE")
    if collection not in COLLECTIONS:
        raise InvalidRequest(f"unknown collection {collection!r} in target {text!r}", code="USAGE")
    return {"collection": collection, "id": id}


def _revision_arg(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise InvalidRequest(f"revision must be an integer, got {value!r}", code="USAGE") from None


def _agree(option: str, given, found, *, what: str):
    if given is not None and given != found:
        raise InvalidRequest(f"{option} {given} does not match the {what} named in the file ({found})",
                             code="ARGUMENT_MISMATCH")


def _latest_audit(db: Database):
    audits = db.heads("audits")
    if not audits:
        return None
    return sorted(audits, key=lambda a: (a.revision or 0, a.id))[-1]


def _diag(message: str):
    print(message, file=sys.stderr)


# -- command implementations -------------------------------------------------------
def cmd_init(args):
    result = initialize(args.db, source_root=args.source_root, title=args.title)
    result["command"] = "init"
    return result


def cmd_attach(args):
    report = Path(args.report).as_posix()
    with Database(args.db, write=True) as db:
        paper = paper_record(db)
        if report in paper.body["report_paths"]:
            return {"command": "attach", "changed": False, "paper_id": paper.id, "report_paths": paper.body["report_paths"],
                    "revision": db.max_revision()}
        packet = get_packet(db, targets=[{"collection": "papers", "id": paper.id}], mode="author")
        body = dict(paper.body)
        body["report_paths"] = list(paper.body["report_paths"]) + [report]
        batch = {"contract_version": CONTRACT_VERSION, "request_id": new_id("request"), "packet_id": packet["packet_id"],
                 "edits": [{"op": "replace", "collection": "papers", "id": paper.id,
                            "expected_version": paper.version, "body": body}]}
        receipt = apply_batch(db, batch)
        return {"command": "attach", "changed": True, "paper_id": paper.id, "report_paths": body["report_paths"],
                "packet_id": packet["packet_id"], "receipt": receipt, "revision": receipt["revision"]}


def cmd_migrate_overview(args):
    result = migrate_overview(args.db, backup=args.backup)
    result["command"] = "migrate-overview"
    return result


def cmd_source_capture(args):
    listing = _load_json(args.files, what="file list")
    if isinstance(listing, dict) and isinstance(listing.get("files"), list):
        listing = listing["files"]
    if not isinstance(listing, list) or not all(isinstance(f, str) and f for f in listing):
        raise InvalidRequest("the file list must be a JSON array of relative paths (or {\"files\": [...]})")
    with Database(args.db, write=True) as db:
        result = capture_sources(db, files=listing, request_id=args.request_id)
    result["command"] = "source capture"
    return result


def cmd_source_anchor(args):
    request = _load_json(args.request, what="anchor request")
    with Database(args.db, write=True) as db:
        result = anchor_sources(db, request=request)
    result["command"] = "source anchor"
    return result


def cmd_source_review(args):
    batch = _load_json(args.request, what="source review batch")
    with Database(args.db, write=True) as db:
        result = review_sources(db, batch=batch)
    return {"command": "source review", "receipt": result}


def cmd_ids(args):
    if args.kind not in ID_KINDS:
        raise InvalidRequest(f"unknown id kind {args.kind!r}; choose one of {list(ID_KINDS)}", code="USAGE")
    if args.count < 1 or args.count > 1000:
        raise InvalidRequest("--count must be between 1 and 1000", code="USAGE")
    return {"command": "ids", "kind": args.kind, "ids": [new_id(args.kind) for _ in range(args.count)]}


def _packet_summary(packet: dict) -> dict:
    return {"packet_id": packet["packet_id"], "packet_version": packet["packet_version"], "mode": packet["mode"],
            "base_revision": packet["base_revision"], "targets": packet["targets"], "records": len(packet["records"]),
            "write_scope": len(packet["write_scope"]), "extends": packet.get("extends"),
            "omitted": len(packet.get("omitted") or []), "truncated": packet.get("truncated", False)}


def cmd_get(args):
    if args.extend is not None:
        if args.target:
            raise InvalidRequest("get --extend takes the added targets from the context request, not --target",
                                 code="USAGE")
        if args.request is None:
            raise InvalidRequest("get --extend needs --request CONTEXT.json", code="USAGE")
        request = _load_json(args.request, what="context request")
        with Database(args.db, write=True) as db:
            packet = get_packet(db, extend=args.extend, request=request)
    else:
        if not args.target:
            raise InvalidRequest("get needs at least one --target COLLECTION:ID (or --extend PACKET_ID)", code="USAGE")
        if args.mode not in MODES:
            raise InvalidRequest(f"--mode must be one of {list(MODES)}", code="USAGE")
        targets = [_parse_target(t) for t in args.target]
        with Database(args.db, write=True) as db:
            packet = get_packet(db, targets=targets, mode=args.mode)
    if args.out is None:
        packet["command"] = "get"
        return packet
    written = _write_json(args.out, packet)
    summary = _packet_summary(packet)
    summary.update({"command": "get", "out": written["path"], "bytes": written["bytes"]})
    return summary


def cmd_apply(args):
    batch = _load_json(args.batch, what="batch")
    with Database(args.db, write=True) as db:
        receipt = apply_batch(db, batch)
    return {"command": "apply", "receipt": receipt, "revision": receipt["revision"]}


def cmd_compare(args):
    batch = _load_json(args.batch, what="comparison batch")
    if isinstance(batch, dict):
        _agree("--packet", args.packet, batch.get("packet_id"), what="packet")
    with Database(args.db, write=True) as db:
        receipt = compare(db, batch=batch)
    return {"command": "compare", "receipt": receipt, "revision": receipt["revision"]}


def cmd_review_submit(args):
    submission = _load_json(args.submission, what="submission envelope")
    try:
        response_bytes = Path(args.response).read_bytes()
    except OSError as exc:
        raise InvalidRequest(f"cannot read worker response {args.response}: {exc}", code="FILE_UNREADABLE") from exc
    with Database(args.db, write=True) as db:
        result = submit_review(db, submission=submission, response_bytes=response_bytes)
    result["command"] = "review submit"
    return result


def cmd_review_map(args):
    mapping = _load_json(args.mapping, what="mapping request")
    if isinstance(mapping, dict):
        _agree("--response", args.response, mapping.get("response_id"), what="response")
    with Database(args.db, write=True) as db:
        result = map_response(db, mapping=mapping)
    result["command"] = "review map"
    return result


def cmd_review_reconcile(args):
    batch = _load_json(args.batch, what="reconciliation batch")
    if isinstance(batch, dict):
        _agree("--packet", args.packet, batch.get("packet_id"), what="packet")
    with Database(args.db, write=True) as db:
        receipt = reconcile(db, batch=batch)
    return {"command": "review reconcile", "receipt": receipt, "revision": receipt["revision"]}


def cmd_qualification_record(args):
    receipt_request = _load_json(args.receipt, what="qualification receipt")
    with Database(args.db, write=True) as db:
        receipt = record_qualification(db, receipt=receipt_request)
    return {"command": "qualification record", "receipt": receipt, "revision": receipt["revision"]}


def cmd_changes(args):
    with Database(args.db) as db:
        result = query_changes(db, since=_revision_arg(args.since), limit=args.limit, offset=args.offset)
    result["command"] = "changes"
    return result


def cmd_status(args):
    with Database(args.db) as db:
        result = query_status(db, audit_id=args.audit, revision=_revision_arg(args.snapshot))
    result["command"] = "status"
    return result


def cmd_migrate(args):
    from .storage import migrate_database
    return {"command": "migrate", **migrate_database(args.db, backup=args.backup)}


def cmd_work_list(args):
    from .controller import list_work
    with Database(args.db) as db:
        result = list_work(db, audit_id=args.audit, focus=_parse_target(args.focus) if args.focus else None,
                           revision=_revision_arg(args.snapshot), limit=args.limit, cursor=args.cursor)
    return {"command": "work list", **result}


def _compact_work(result):
    return {k: v for k, v in result.items()
            if k not in ("packet", "manifest", "scaffold", "worker_guidance", "coordinator_guidance")}


def cmd_work_prepare(args):
    from .controller import prepare_work, write_artifacts
    with Database(args.db, write=True) as db:
        result = prepare_work(db, audit_id=args.audit, mode=args.mode,
                              focus=_parse_target(args.focus) if args.focus else None, task_ids=args.task,
                              exclude_task_ids=args.exclude_task, max_units=args.max_units,
                              max_bytes=args.max_bytes, allow_provisional=args.allow_provisional, route_id=args.route)
        if result.get("prepared"):
            try:
                result["files"] = write_artifacts(db, result, args.out)
            except CoreError as exc:
                exc.records.append({"packet_id": result["packet_id"], "next_action": "work inspect --packet"})
                raise
    return {"command": "work prepare", **_compact_work(result)}


def cmd_work_extend(args):
    from .controller import extend_work, read_bounded, write_artifacts
    from .canonical import load_json_bytes
    try:
        request = load_json_bytes(read_bounded(args.request, 65536))
    except (UnicodeError, ValueError) as exc:
        raise InvalidRequest(f"context request is not valid UTF-8 JSON: {exc}", code="JSON_INVALID") from exc
    with Database(args.db, write=True) as db:
        result = extend_work(db, packet_id=args.packet, request=request)
        if result.get("prepared"):
            try:
                result["files"] = write_artifacts(db, result, args.out)
            except CoreError as exc:
                exc.records.append({"packet_id": result["packet_id"], "next_action": "work inspect --packet"})
                raise
    return {"command": "work extend", **_compact_work(result)}


def cmd_work_submit(args):
    from .controller import ENVELOPE_LIMIT, RESPONSE_LIMIT, read_bounded, submit_work
    try:
        envelope = read_bounded(args.submission, ENVELOPE_LIMIT)
        response = read_bounded(args.response, RESPONSE_LIMIT)
    except CoreError as exc:
        return {"command": "work submit", "stored": False, "exit_code": exc.exit_code, **exc.to_json()}
    with Database(args.db, write=True) as db:
        result = submit_work(db, envelope_bytes=envelope, response_bytes=response)
    return {"command": "work submit", **result}


def cmd_work_inspect(args):
    from .controller import inspect_work, write_artifacts
    if args.audit and args.out:
        raise InvalidRequest("history inspection has no payload output; choose a request or packet")
    with Database(args.db) as db:
        result = inspect_work(db, request_id=args.request, packet_id=args.packet, audit_id=args.audit,
                              limit=args.limit, cursor=args.cursor)
        if args.out:
            result["files"] = write_artifacts(db, result, args.out)
        if "manifest" in result:
            manifest = result["manifest"]
            result["assignment"] = {"audit_id": manifest["work"]["audit_id"], "mode": manifest["mode"],
                                    "task_ids": [t["id"] for t in manifest["work"]["tasks"]]}
    return {"command": "work inspect", **_compact_work(result)}


def cmd_validate(args):
    with Database(args.db) as db:
        result = validate_snapshot(db, revision=_revision_arg(args.snapshot))
    result["command"] = "validate"
    return result


def cmd_checkpoint(args):
    with Database(args.db, write=True) as db:
        audit_id = args.audit
        if audit_id is None:
            audit = _latest_audit(db)
            audit_id = None if audit is None else audit.id
        projection = build_projection(db, audit_id=audit_id)
        result = publish_report(db, projection=projection, output=args.out, release=False)
    result.update({"command": "checkpoint", "audit_id": audit_id,
                   "process_complete": projection["summary"]["progress"]["process_complete"]})
    return result


def _release_directory(path) -> Path:
    directory = Path(path)
    if directory.exists():
        if not directory.is_dir():
            raise InvalidRequest(f"release output {directory} exists and is not a directory")
        if any(directory.iterdir()):
            raise InvalidRequest(f"release output directory {directory} is not empty")
    else:
        directory.mkdir(parents=True)
    return directory


def cmd_release(args):
    with Database(args.db, write=True) as db:
        revision = db.max_revision()
        validation = validate_snapshot(db, revision=revision)
        if not validation["ok"]:
            raise InvalidRequest("release refused: the snapshot fails validation", code="RELEASE_BLOCKED",
                                 records=validation["errors"])
        state = query_status(db, audit_id=args.audit, revision=revision)
        if not state["process_complete"]:
            blockers = [{"kind": "obligation", "id": oid} for oid in state["obligations"]["unsatisfied"]]
            blockers += [{"kind": "problem", "detail": p} for p in state["problems"]]
            blockers += [{"kind": "source_limit", "detail": s} for s in state["source_limits"]]
            blockers += [{"kind": "independent_review", "target": k, "indicator": v}
                         for k, v in state["independent"].items() if v == "disputed"]
            raise InvalidRequest(f"release refused: audit {args.audit} is not process-complete at revision {revision}",
                                 code="RELEASE_BLOCKED", records=blockers)
        directory = _release_directory(args.out)
        projection = build_projection(db, revision=revision, audit_id=args.audit)
        published = publish_report(db, projection=projection, output=directory / "report.html", release=True)
        exported = write_export(db, output=directory / "export.json", revision=revision, history=True)
        receipt = {"command": "release", "core_version": CORE_VERSION, "storage_format": STORAGE_FORMAT,
                   "contract": CONTRACT_NAME, "projection_version": PROJECTION_VERSION, "revision": revision,
                   "audit_id": args.audit, "paper_id": state["paper"]["id"], "process_complete": True,
                   "publication": published, "export": exported, "validation": {"ok": True, "warnings": validation["warnings"]},
                   "status": {"progress": state["progress"], "assessments": state["assessments"],
                              "independent": state["independent"], "findings": state["findings"]}}
        _write_json(directory / "receipt.json", receipt)
        for name in ("report.html", "export.json", "receipt.json"):
            try:
                os.chmod(directory / name, 0o444)
            except OSError:  # pragma: no cover - platform dependent
                pass
    receipt["directory"] = str(directory)
    receipt["files"] = ["report.html", "export.json", "receipt.json"]
    return receipt


def cmd_export(args):
    with Database(args.db) as db:
        result = write_export(db, output=args.out, revision=_revision_arg(args.snapshot), history=args.history)
    result["command"] = "export"
    return result


def cmd_backup(args):
    with Database(args.db) as db:
        result = db.backup(args.out)
    result["command"] = "backup"
    return result


def cmd_import_legacy(args):
    result = import_legacy(args.audit_folder, db_path=args.db, mapping_path=args.map)
    result["command"] = "import-legacy"
    return result


def cmd_telemetry_record(args):
    details = {}
    if args.details is not None:
        text = args.details
        if Path(text).is_file():
            details = _load_json(text, what="event details")
        else:
            try:
                details = json.loads(text)
            except ValueError as exc:
                raise InvalidRequest(f"--details must be a JSON object or a path to one: {exc}", code="USAGE") from exc
    with Database(args.db, write=True) as db:
        result = record_event(db, run_id=args.run_id, stage=args.stage, elapsed_ms=args.elapsed_ms, details=details,
                              started_at=args.started_at)
    result["command"] = "telemetry record"
    return result


def cmd_telemetry_summary(args):
    with Database(args.db) as db:
        result = summarize(db, run_id=args.run_id)
        if args.events:
            result["event_list"] = events(db, run_id=args.run_id)
    result["command"] = "telemetry summary"
    return result


def cmd_version(args):
    from .bundle import verify_bundle
    return {"command": "version", "core_version": CORE_VERSION, "storage_format": STORAGE_FORMAT,
            "contract_version": CONTRACT_VERSION, "contract": CONTRACT_NAME, "packet_version": PACKET_VERSION,
            "projection_version": PROJECTION_VERSION, "python": sys.version.split()[0],
            "bundle": verify_bundle()}


# -- parser ---------------------------------------------------------------------------
def _db_arg(parser, *, run_id=True):
    parser.add_argument("db", help="path of the paper database")
    if run_id:
        parser.add_argument("--run-id", default=None,
                            help="record a 'command' telemetry event for this invocation under RUN_ID")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG, description="Proofcheck paper database commands (one JSON object on stdout).")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("init", help="create a database with its paper record")
    _db_arg(p)
    p.add_argument("--source-root", required=True)
    p.add_argument("--title", required=True)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("attach", help="register a report path on the paper record")
    _db_arg(p)
    p.add_argument("--report", required=True)
    p.set_defaults(func=cmd_attach)

    p = sub.add_parser("migrate-overview", help="upgrade an archify overview database in place (backup first)")
    _db_arg(p)
    p.add_argument("--backup", required=True)
    p.set_defaults(func=cmd_migrate_overview)

    p = sub.add_parser("migrate", help="upgrade native storage in place after an explicit SQLite backup")
    _db_arg(p)
    p.add_argument("--backup", required=True)
    p.set_defaults(func=cmd_migrate)

    work = sub.add_parser("work", help="bounded work preparation, submission and inspection").add_subparsers(
        dest="subcommand", metavar="ACTION", required=True)
    p = work.add_parser("list", help="derive current tasks and coordinator actions")
    _db_arg(p)
    p.add_argument("--audit", required=True)
    p.add_argument("--focus")
    p.add_argument("--snapshot")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--cursor")
    p.set_defaults(func=cmd_work_list)
    p = work.add_parser("prepare", help="prepare one coherent assignment without dispatching a model")
    _db_arg(p)
    p.add_argument("--audit", required=True)
    p.add_argument("--mode", choices=("primary", "independent", "reconcile"), required=True)
    p.add_argument("--focus")
    p.add_argument("--route", help="exact argument for supplied-route independent review")
    p.add_argument("--task", action="append", default=[])
    p.add_argument("--exclude-task", action="append", default=[])
    p.add_argument("--max-units", type=int, default=5)
    p.add_argument("--max-bytes", type=int, default=131072)
    p.add_argument("--allow-provisional", action="store_true")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_work_prepare)
    p = work.add_parser("extend", help="add captured neutral source to an independent work assignment")
    _db_arg(p)
    p.add_argument("--packet", required=True)
    p.add_argument("--request", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_work_extend)
    p = work.add_parser("submit", help="preserve and validate one structured response")
    _db_arg(p)
    p.add_argument("--submission", required=True)
    p.add_argument("--response", required=True)
    p.set_defaults(func=cmd_work_submit)
    p = work.add_parser("inspect", help="inspect saved input, a preparation, or bounded audit history")
    _db_arg(p)
    choice = p.add_mutually_exclusive_group(required=True)
    choice.add_argument("--request")
    choice.add_argument("--packet")
    choice.add_argument("--audit")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--cursor")
    p.add_argument("--out")
    p.set_defaults(func=cmd_work_inspect)

    source = sub.add_parser("source", help="capture, anchor and review sources").add_subparsers(dest="subcommand",
                                                                                                 metavar="ACTION")
    source.required = True
    p = source.add_parser("capture", help="capture listed files under the source root")
    _db_arg(p)
    p.add_argument("--files", required=True, help="JSON array of relative paths")
    p.add_argument("--request-id", default=None)
    p.set_defaults(func=cmd_source_capture)
    p = source.add_parser("anchor", help="create or rebind anchors from a locator request")
    _db_arg(p)
    p.add_argument("--request", required=True)
    p.set_defaults(func=cmd_source_anchor)
    p = source.add_parser("review", help="record source reviews and source issues")
    _db_arg(p)
    p.add_argument("--request", required=True)
    p.set_defaults(func=cmd_source_review)

    p = sub.add_parser("ids", help="mint identifiers")
    p.add_argument("--kind", required=True, help=f"one of {', '.join(ID_KINDS)}")
    p.add_argument("--count", type=int, default=1)
    p.set_defaults(func=cmd_ids)

    p = sub.add_parser("template", help="generate one collection's uncommitted authoring shape")
    _db_arg(p)
    p.add_argument("--collection", required=True, choices=COLLECTIONS)
    p.add_argument("--packet", required=True)
    p.add_argument("--id", default=None, help="existing shared identity for a same-ID extension")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_template)

    p = sub.add_parser("get", help="assemble a packet")
    _db_arg(p)
    p.add_argument("--target", action="append", default=[], help="COLLECTION:ID (repeatable)")
    p.add_argument("--mode", default="author", help=f"one of {', '.join(MODES)}")
    p.add_argument("--extend", default=None, metavar="PACKET_ID")
    p.add_argument("--request", default=None, metavar="CONTEXT.json")
    p.add_argument("--out", default=None, metavar="PACKET.json")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("apply", help="accept an edit batch against a packet")
    _db_arg(p)
    p.add_argument("--batch", required=True)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("compare", help="record source-versus-record observations")
    _db_arg(p)
    p.add_argument("--packet", default=None)
    p.add_argument("--batch", required=True)
    p.set_defaults(func=cmd_compare)

    review = sub.add_parser("review", help="independent review submission, mapping, reconciliation").add_subparsers(
        dest="subcommand", metavar="ACTION")
    review.required = True
    p = review.add_parser("submit", help="preserve a worker response and derive independent checks")
    _db_arg(p)
    p.add_argument("--submission", required=True)
    p.add_argument("--response", required=True)
    p.set_defaults(func=cmd_review_submit)
    p = review.add_parser("map", help="map pending judgments of a needs_revision response")
    _db_arg(p)
    p.add_argument("--response", default=None, metavar="RESPONSE_ID")
    p.add_argument("--mapping", required=True)
    p.set_defaults(func=cmd_review_map)
    p = review.add_parser("mapping-template", help="scaffold coordinator mapping with worker and mapping packet identities")
    _db_arg(p)
    p.add_argument("--response", required=True)
    p.add_argument("--packet", required=True)
    p.add_argument("--judgment", type=int, action="append", default=None)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_review_mapping_template)
    p = review.add_parser("reconcile", help="record reconciliations against a reconcile packet")
    _db_arg(p)
    p.add_argument("--packet", default=None)
    p.add_argument("--batch", required=True)
    p.set_defaults(func=cmd_review_reconcile)

    qualification = sub.add_parser("qualification", help="reviewer qualification receipts").add_subparsers(
        dest="subcommand", metavar="ACTION")
    qualification.required = True
    p = qualification.add_parser("record", help="store a qualification receipt")
    _db_arg(p)
    p.add_argument("--receipt", required=True)
    p.set_defaults(func=cmd_qualification_record)

    p = sub.add_parser("changes", help="record versions committed after a revision")
    _db_arg(p)
    p.add_argument("--since", required=True, metavar="REV")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_changes)

    p = sub.add_parser("status", help="progress and presentation state")
    _db_arg(p)
    p.add_argument("--audit", default=None)
    p.add_argument("--snapshot", default=None, metavar="REV")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("validate", help="check a snapshot (exit 2 when it fails)")
    _db_arg(p)
    p.add_argument("--snapshot", default=None, metavar="REV")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("checkpoint", help="render a working report")
    _db_arg(p)
    p.add_argument("--out", required=True, metavar="HTML")
    p.add_argument("--audit", default=None)
    p.set_defaults(func=cmd_checkpoint)

    p = sub.add_parser("release", help="render the release report and export for a process-complete audit")
    _db_arg(p)
    p.add_argument("--audit", required=True)
    p.add_argument("--out", required=True, metavar="DIRECTORY")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("export", help="write the export JSON of a snapshot")
    _db_arg(p)
    p.add_argument("--out", required=True, metavar="JSON")
    p.add_argument("--snapshot", default=None, metavar="REV")
    p.add_argument("--history", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("backup", help="copy the database with the SQLite backup API")
    _db_arg(p)
    p.add_argument("--out", required=True, metavar="BACKUP")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("import-legacy", help="import a stat-paper-proofcheck v1.5 audit folder into a new database")
    p.add_argument("audit_folder")
    p.add_argument("--db", required=True, metavar="NEW_DB")
    p.add_argument("--map", required=True, metavar="MAPPING.json")
    p.set_defaults(func=cmd_import_legacy, run_id=None)

    telemetry = sub.add_parser("telemetry", help="observational timing events").add_subparsers(dest="subcommand",
                                                                                                metavar="ACTION")
    telemetry.required = True
    p = telemetry.add_parser("record", help="append one timing event")
    _db_arg(p, run_id=False)
    p.add_argument("--run-id", required=True)
    p.add_argument("--stage", required=True, help=f"one of {', '.join(STAGES)}")
    p.add_argument("--elapsed-ms", type=int, default=None)
    p.add_argument("--details", default=None, help="JSON object text or a path to a JSON file")
    p.add_argument("--started-at", default=None)
    p.set_defaults(func=cmd_telemetry_record, telemetry_own=True)
    p = telemetry.add_parser("summary", help="per-stage and per-run totals (observational only)")
    _db_arg(p, run_id=False)
    p.add_argument("--run-id", default=None)
    p.add_argument("--events", action="store_true", help="include the individual events")
    p.set_defaults(func=cmd_telemetry_summary, telemetry_own=True)

    p = sub.add_parser("version", help="core and format versions")
    p.set_defaults(func=cmd_version)
    return parser


# -- entry ------------------------------------------------------------------------------
def _emit(payload):
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _record_command_event(args, *, started, exit_code, error_code=None):
    run_id = getattr(args, "run_id", None)
    if not run_id or getattr(args, "telemetry_own", False) or getattr(args, "db", None) is None:
        return
    elapsed_ms = int(round((time.monotonic() - started) * 1000))
    details = {"command": args.command, "subcommand": getattr(args, "subcommand", None), "exit_code": exit_code,
               "outcome": "ok" if exit_code == 0 else "error", "core_version": CORE_VERSION}
    if error_code is not None:
        details["error_code"] = error_code
    try:
        with Database(args.db, write=True) as db:
            record_event(db, run_id=run_id, stage="command", elapsed_ms=elapsed_ms, details=details)
    except Exception as exc:  # telemetry never changes the command's outcome
        _diag(f"telemetry: could not record command event: {exc}")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (ValueError, AttributeError):  # pragma: no cover - exotic stdout replacements
            pass
    parser = build_parser()
    args = None
    started = time.monotonic()
    try:
        args = parser.parse_args(argv)
        payload = args.func(args)
        _emit(payload)
        exit_code = payload.get("exit_code", 0) if args.command == "work" else 0
        if args.command == "validate" and not payload.get("ok", True):
            exit_code = 2
        _record_command_event(args, started=started, exit_code=exit_code)
        return exit_code
    except CoreError as exc:
        _emit(exc.to_json())
        _diag(f"{exc.code}: {exc.message}")
        for record in exc.records[:20]:
            _diag(f"  {json.dumps(record, ensure_ascii=False, default=str)}")
        if args is not None:
            _record_command_event(args, started=started, exit_code=exc.exit_code, error_code=exc.code)
        return exc.exit_code
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - last resort: still one JSON object on stdout
        _emit({"error": {"code": "INTERNAL", "message": f"{type(exc).__name__}: {exc}", "records": []}})
        traceback.print_exc(file=sys.stderr)
        if args is not None:
            _record_command_event(args, started=started, exit_code=2, error_code="INTERNAL")
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
