"""Shared proofcheck core: storage format 3, record contract 3.

One maintained source lives at ``shared/paper_core``. Skill packages receive
byte-identical generated bundles built by ``tools/build_paper_core_bundles.py``.
New-format code never imports the legacy ``proofcheck.py`` monolith.
"""
from __future__ import annotations

CORE_VERSION = "2.0.0"
STORAGE_FORMAT = 3
CONTRACT_VERSION = 3
CONTRACT_NAME = "proofcheck-records/3"
PACKET_VERSION = 2
PROJECTION_VERSION = 2
PROTOCOL_VERSION = "item-audit/1"
# Feature names a database may require; every open checks the stored list is a
# subset of what this core supports. Compare names, never bare digits.
SUPPORTED_FEATURES = (
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
)
STORAGE_FORMATS_READABLE = (2, 3)
STORAGE_FORMATS_WRITABLE = (3,)
LEGACY_OVERVIEW_FORMAT = "archify-paper-database-1"
LEGACY_OVERVIEW_SCHEMA_VERSIONS = (3,)
