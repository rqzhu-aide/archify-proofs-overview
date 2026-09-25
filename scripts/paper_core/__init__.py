"""Shared proofcheck core: storage format 4, record contract 4.

One maintained source lives at ``shared/paper_core``. Skill packages receive
byte-identical generated bundles built by ``tools/build_paper_core_bundles.py``.
New-format code never imports the legacy ``proofcheck.py`` monolith.
"""
from __future__ import annotations

CORE_VERSION = "2.2.2"
STORAGE_FORMAT = 4
CONTRACT_VERSION = 4
CONTRACT_NAME = "proofcheck-records/4"
PACKET_VERSION = 2
PROJECTION_VERSION = 2
PROTOCOL_VERSION = "item-audit/1"
# Feature names a database may require; every open checks the stored list is a
# subset of what this core supports. Compare names, never bare digits.
DEFAULT_FEATURES = (
    "records/3",
    "packets/1",
    "packets/2",
    "work-submissions/1",
    "audits/1",
    "independent-review/1",
    "projection/1",
    "projection/2",
    # Overview bridging: intermediate item kinds equation/claim/derivation and
    # provenance groups with null argument_id/scope_id (record-contract 3).
    "overview-bridge/1",
    "sql-superset/1",
    "records/4",
    "route-review/1",
)
WORK_CONTEXT_EXTENSION_FEATURE = "work-context-extension/1"
SUPPORTED_FEATURES = DEFAULT_FEATURES + (WORK_CONTEXT_EXTENSION_FEATURE,)
STORAGE_FORMATS_READABLE = (2, 3, 4)
STORAGE_FORMATS_WRITABLE = (4,)
LEGACY_OVERVIEW_FORMAT = "archify-paper-database-1"
LEGACY_OVERVIEW_SCHEMA_VERSIONS = (3,)
