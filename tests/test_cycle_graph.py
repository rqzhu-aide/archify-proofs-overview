"""Cycles retain a complete directed drawing and navigable source records.

These exercise computed geometry and the viewer's actual reachability function,
not browser layout or pointer hit testing.
"""
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import paper_records

NODE = shutil.which("node")


class Drawing(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes, self.uses, self.routes = [], [], {}
        self.feed(html)

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        if tag == "g" and "data-node-id" in attrs:
            self.nodes.append(attrs["data-node-id"])
        if tag == "path" and "data-edge-id" in attrs:
            self.uses.append((attrs["data-edge-id"], attrs["data-edge-from"], attrs["data-edge-to"]))
            self.routes[attrs["data-edge-id"]] = attrs["d"]


@unittest.skipUnless(NODE, "A shared Node installation is required.")
class CycleGraphTests(unittest.TestCase):
    def render(self, names, pairs):
        items = [{"id": name, "kind": "lemma", "label": f"Lemma {index + 1}",
                  "caption": name, "statement_html": f"Statement {name}"}
                 for index, name in enumerate(names)]
        uses = [{"id": f"use-{index}", "from": start, "to": end, "type": "dependency",
                 "reason": f"Recorded contribution {index}"}
                for index, (start, end) in enumerate(pairs)]
        cycles = paper_records._graph_cycles(items, uses)
        data = {"schema_version": 3, "title": "Cycle fixture", "scope": "Synthetic relationships.",
                "graph_mode": "cyclic" if cycles else "dag", "graph_cycles": cycles,
                "items": items, "uses": uses, "details": [], "detail_uses": []}
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / "prepared.json", Path(temp) / "overview.html"
            source.write_text(json.dumps(data), encoding="utf-8")
            result = subprocess.run([NODE, str(SKILL / "scripts/render.mjs"), str(source), str(target)],
                                    text=True, encoding="utf-8", capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            html = target.read_text(encoding="utf-8")
        receipt, drawing = json.loads(result.stdout), Drawing(html)
        self.assertEqual(sorted(drawing.nodes), sorted(names))
        self.assertEqual(sorted(drawing.uses), sorted((row["id"], row["from"], row["to"]) for row in uses))
        self.assertEqual(receipt["graph_preservation"]["representation"], "svg")
        self.assertEqual(receipt["graph_preservation"]["status"], "pass")
        self.assertEqual(receipt["geometry"]["status"], "pass")
        self.assertIn("recorded_route_endpoints", receipt["geometry"]["checks"])
        return data, receipt, html, drawing

    def test_cycle_parallel_self_and_unaffected_components_all_remain_drawn(self):
        data, receipt, html, drawing = self.render(
            ["a", "b", "c", "d", "e", "f", "isolated"],
            [("a", "b"), ("a", "b"), ("b", "a"), ("b", "b"), ("b", "c"),
             ("d", "e"), ("e", "d"), ("e", "f")])
        self.assertEqual(receipt["graph_mode"], "cyclic")
        self.assertEqual([group["item_ids"] for group in data["graph_cycles"]], [["a", "b"], ["d", "e"]])
        self.assertEqual(data["graph_cycles"][0]["use_ids"], ["use-0", "use-1", "use-2", "use-3"])
        self.assertNotEqual(drawing.routes["use-0"], drawing.routes["use-1"])
        self.assertIn("Recorded cycles (2): all connections remain visible", html)
        self.assertIn("it does not establish a circular proof", html)
        for row in data["uses"]:
            self.assertIn(f'<template id="proof-use-{row["id"]}">', html)

    def test_terminal_cycle_and_self_only_graph_have_reading_entry_points(self):
        for names, pairs in [(["a", "b"], [("a", "b"), ("b", "a")]),
                             (["a"], [("a", "a"), ("a", "a")])]:
            with self.subTest(names=names):
                _, _, html, drawing = self.render(names, pairs)
                payload = json.loads(re.search(r'<script id="proof-overview-data" type="application/json">(.*?)</script>', html).group(1))
                self.assertEqual(payload["main_items"], names)
                self.assertEqual(len(set(drawing.routes.values())), len(pairs))
                self.assertIn("Results and terminal cycle groups", html)

    def test_interlocking_cycles_and_skipped_layers_pass_geometry(self):
        names = [f"n{index}" for index in range(12)]
        pairs = [(f"n{index}", f"n{index+1}") for index in range(11)]
        pairs += [("n4", "n1"), ("n7", "n3"), ("n8", "n8"), ("n10", "n9"),
                  ("n0", "n11"), ("n2", "n11"), ("n7", "n10"), ("n9", "n11")]
        self.render(names, pairs)

    def test_dense_cyclic_group_preserves_each_path(self):
        names = [f"n{index}" for index in range(8)]
        pairs = [(start, end) for start in names for end in names]
        _, receipt, _, drawing = self.render(names, pairs)
        self.assertEqual(receipt["graph_preservation"]["rendered_uses"], 64)
        self.assertEqual(len(set(drawing.routes.values())), 64)

    def test_dag_positions_remain_the_existing_layout(self):
        _, receipt, html, _ = self.render(["a", "b"], [("a", "b")])
        self.assertEqual(receipt["graph_mode"], "dag")
        payload = json.loads(re.search(r'<script id="proof-overview-data" type="application/json">(.*?)</script>', html).group(1))
        self.assertEqual(payload["items"], [{"id": "a", "x": 121, "y": 68}, {"id": "b", "x": 396, "y": 68}])

    def test_actual_viewer_reachability_terminates_and_keeps_cycle_edges(self):
        # Execute the bundled implementation, rather than a reimplementation.
        template = (SKILL / "assets/archify/template.html").read_text(encoding="utf-8")
        start = template.index("function computeReachability(")
        end = template.index("\n      function ", start + 9)
        function = template[start:end]
        relationships = [{"key": str(index), "from": source, "to": target} for index, (source, target) in enumerate(
            [("a", "b"), ("a", "b"), ("b", "a"), ("b", "b"), ("b", "c"), ("x", "y")])]
        code = function + "\nconst edges=" + json.dumps(relationships) + ";\nconsole.log(JSON.stringify([computeReachability('a','downstream',edges),computeReachability('c','upstream',edges)]));"
        result = subprocess.run([NODE, "--input-type=module", "-e", code], text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for reach in json.loads(result.stdout):
            self.assertEqual(sorted(reach["nodeIds"]), ["a", "b", "c"])
            self.assertEqual(reach["edgeKeys"], ["0", "1", "2", "3", "4"])

    def test_actual_viewer_keeps_self_and_parallel_uses_individually_selectable(self):
        template = (SKILL / "assets/archify/template.html").read_text(encoding="utf-8")
        start = template.index("function relationshipEdgeShapes(")
        end = template.index("function relationshipRecordForKey(", start)
        functions = template[start:end]
        records = [{"id": f"use-{index}", "from": source, "to": target} for index, (source, target) in enumerate(
            [("a", "b"), ("a", "b"), ("b", "a"), ("b", "b")])]
        code = "const rows=" + json.dumps(records) + ";\n" + """
        function nodes(){return ['a','b'].map(id=>({getAttribute:()=>id}));}
        function nodeLabel(node,id){return id;}
        function edges(){return rows.map((row,index)=>({tagName:'path',getAttribute:key=>({
          'data-edge-from':row.from,'data-edge-to':row.to,'data-edge-id':row.id,
          'data-edge-key':String(index),'data-edge-label':'Recorded use'
        })[key]}));}
        """ + functions + "\nconsole.log(JSON.stringify(relationshipHitRecords().map(({id,from,to})=>({id,from,to}))));"
        result = subprocess.run([NODE, "--input-type=module", "-e", code], text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), records)


if __name__ == "__main__":
    unittest.main()
