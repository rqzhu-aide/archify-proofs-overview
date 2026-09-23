"""Exercise the shipped entry point without development-tree imports."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


SKILL = Path(__file__).resolve().parents[1]


class StandalonePackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.scripts = self.base / "installed-skill" / "scripts"
        shutil.copytree(SKILL / "scripts", self.scripts,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.entry = self.scripts / "paper_database.py"
        (self.base / "paper.tex").write_text(
            "\\begin{theorem}\\label{thm:constant}\n"
            "A constant random variable has variance zero.\n"
            "\\end{theorem}\n", encoding="utf-8")
        self.seed = self.base / "seed.json"
        self.seed.write_text(json.dumps({
            "schema_version": 3,
            "title": "Constant variance",
            "scope": "One selected theorem from a synthetic source.",
            "source": {"title": "Synthetic paper", "file": "paper.tex"},
            "main_items": ["constant"],
            "items": [{
                "id": "constant", "kind": "theorem", "label": "Theorem 1",
                "caption": "Variance of a constant",
                "statement": {"text": "A constant random variable has variance zero.",
                              "form": "synopsis"},
                "source": {"label": "thm:constant", "start_line": 1,
                           "end_line": 3, "page": 1},
            }],
            "uses": [],
        }), encoding="utf-8")
        self.database = self.base / "paper.sqlite"

    def invoke(self, *arguments, code=None, python_path=None):
        command = [sys.executable, "-X", "utf8", "-B"]
        command += ["-c", code] if code else [str(self.entry)]
        command += [str(argument) for argument in arguments]
        # A user's PYTHONPATH must not make an isolated-copy test import the
        # development tree accidentally. Each CLI call has its own process.
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"PYTHONPATH", "PYTHONHOME"}}
        if python_path is not None:
            environment["PYTHONPATH"] = str(python_path)
        return subprocess.run(command, cwd=self.base, env=environment,
                              capture_output=True, text=True,
                              encoding="utf-8", timeout=30)

    def parse_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_clean_failure(self, result):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        error = json.loads(result.stderr)["error"]
        self.assertRegex(error.lower(), "reinstall|restart|bundl|fresh python process")
        self.assertFalse(self.database.exists())
        return error

    def test_public_init_uses_bundle_and_preserves_supplemental_tex_page(self):
        # This occupies the exact ancestor location previously preferred by
        # _common_core. A release must behave like the same installed bytes.
        foreign = self.base / "shared" / "paper_core"
        foreign.mkdir(parents=True)
        (foreign / "__init__.py").write_text(
            "raise RuntimeError('UNEXPECTED_ANCESTOR_CORE')\n", encoding="utf-8")
        receipt = self.parse_success(self.invoke(
            "init", self.database, self.seed, "--focused"))
        self.assertEqual(receipt["backend"], "paper_core")
        self.assertEqual(receipt["storage_format"], 4)
        self.assertEqual(receipt["core_version"], "2.1.0")
        self.assertEqual(receipt["authoring_profile"], "focused")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute(
                "SELECT value FROM metadata WHERE key='storage_format'"
            ).fetchone()[0], "4")
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        output = self.base / "export.json"
        self.parse_success(self.invoke("export", self.database, output))
        data = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(data["main_items"], ["constant"])
        anchor = data["anchors"][0]
        self.assertEqual(anchor["locator"]["page"], 1)
        self.assertEqual(anchor["locator"]["start_line"], 1)
        self.assertEqual(anchor["locator"]["end_line"], 3)

    def test_missing_required_module_fails_before_creating_database(self):
        foreign_root = self.base / "foreign"
        foreign = foreign_root / "paper_core"
        foreign.mkdir(parents=True)
        (foreign / "__init__.py").write_text(
            "raise RuntimeError('UNEXPECTED_EXTERNAL_CORE')\n", encoding="utf-8")
        for filename in ("overview.py", "__init__.py"):
            with self.subTest(missing=filename):
                module = self.scripts / "paper_core" / filename
                contents = module.read_bytes()
                module.unlink()
                try:
                    error = self.assert_clean_failure(self.invoke(
                        "init", self.database, self.seed, "--focused",
                        python_path=foreign_root))
                    self.assertIn(filename, error)
                finally:
                    module.write_bytes(contents)

    def test_preloaded_foreign_core_is_rejected_without_silent_switch(self):
        foreign_root = self.base / "foreign"
        foreign = foreign_root / "paper_core"
        foreign.mkdir(parents=True)
        (foreign / "__init__.py").write_text(
            "CORE_VERSION = 'foreign'\nSTORAGE_FORMAT = 4\n", encoding="utf-8")
        # Embedding tools may have imported another skill's paper_core before
        # invoking this CLI. The error must explain how to obtain a clean run.
        code = (
            "import runpy, sys\n"
            "sys.path.insert(0, sys.argv.pop(1))\n"
            "import paper_core\n"
            "entry = sys.argv.pop(1)\n"
            "sys.path.insert(0, str(__import__('pathlib').Path(entry).parent))\n"
            "sys.argv[0] = entry\n"
            "runpy.run_path(entry, run_name='__main__')\n"
        )
        self.assert_clean_failure(self.invoke(
            foreign_root, self.entry, "init", self.database, self.seed,
            "--focused", code=code))


if __name__ == "__main__":
    unittest.main()
