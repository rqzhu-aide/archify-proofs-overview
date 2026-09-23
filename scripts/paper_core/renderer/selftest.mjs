// Renderer self-test: vendored-file hashes, fixture renders, and explicit cycle modes.
//
// Usage: node selftest.mjs            (exit 0 when every check passes, 1 otherwise)
// Prints one JSON object with the individual check results.
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, mkdtempSync, rmSync, existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const RENDERER = path.join(HERE, 'render_projection.mjs');
const NOTICES = path.join(HERE, 'THIRD_PARTY_NOTICES.md');
const FIXTURES = path.join(HERE, 'fixtures');
const PASSING = ['dag_small.json', 'index_fallback.json', 'long_math.json'];
const FAILING = [{ file: 'cycle_dag.json', stage: 'layout' }];

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function noticeTable() {
  const rows = [];
  for (const line of readFileSync(NOTICES, 'utf8').split(/\r?\n/)) {
    const match = /^\|\s*`([^`]+)`\s*\|[^|]*\|[^|]*\|\s*`([0-9a-f]{64})`\s*\|/.exec(line);
    if (match) rows.push({ file: match[1], sha256: match[2] });
  }
  return rows;
}

function checkHashes(results) {
  const rows = noticeTable();
  if (rows.length === 0) results.push({ check: 'notices', ok: false, error: 'no hash rows found in THIRD_PARTY_NOTICES.md' });
  for (const row of rows) {
    const target = path.join(HERE, ...row.file.split('/'));
    if (!existsSync(target)) {
      results.push({ check: `hash:${row.file}`, ok: false, error: 'vendored file missing' });
      continue;
    }
    const actual = sha256(readFileSync(target));
    results.push({ check: `hash:${row.file}`, ok: actual === row.sha256, expected: row.sha256, actual });
  }
}

function runRenderer(input, output) {
  const run = spawnSync(process.execPath, [RENDERER, input, output], { encoding: 'utf8' });
  let stdout = null;
  let stderr = null;
  try { stdout = run.stdout.trim() ? JSON.parse(run.stdout) : null; } catch { stdout = { unparsable: run.stdout }; }
  try { stderr = run.stderr.trim() ? JSON.parse(run.stderr) : null; } catch { stderr = { unparsable: run.stderr }; }
  return { status: run.status, stdout, stderr };
}

function checkFixtures(results, workdir) {
  for (const name of PASSING) {
    const output = path.join(workdir, name.replace(/\.json$/, '.html'));
    const run = runRenderer(path.join(FIXTURES, name), output);
    const receipt = run.stdout || {};
    const problems = [];
    if (run.status !== 0) problems.push(`exit ${run.status}: ${JSON.stringify(run.stderr)}`);
    if (!receipt.representation || receipt.representation.status !== 'pass') problems.push('representation check did not pass');
    if (!receipt.geometry || receipt.geometry.status === 'fail') problems.push('geometry check failed');
    if (run.status === 0) {
      const actual = sha256(readFileSync(output));
      if (actual !== receipt.artifact_sha256) problems.push('artifact hash does not match the written file');
    }
    results.push({ check: `render:${name}`, ok: problems.length === 0, problems, layout_mode: receipt.layout_mode,
      nodes: receipt.nodes, connections: receipt.connections });
  }
  for (const entry of FAILING) {
    const output = path.join(workdir, entry.file.replace(/\.json$/, '.html'));
    const run = runRenderer(path.join(FIXTURES, entry.file), output);
    const problems = [];
    if (run.status !== 1) problems.push(`expected exit 1, got ${run.status}`);
    if (!run.stderr || run.stderr.stage !== entry.stage) problems.push(`expected failure stage ${entry.stage}, got ${JSON.stringify(run.stderr && run.stderr.stage)}`);
    if (existsSync(output)) problems.push('a failed render left an output file behind');
    results.push({ check: `fail:${entry.file}`, ok: problems.length === 0, problems });
  }
  // The same cyclic dataset is drawable when it declares its actual layout
  // mode. The dag declaration above must still fail, rather than change mode.
  const cyclic = JSON.parse(readFileSync(path.join(FIXTURES, 'cycle_dag.json'), 'utf8'));
  cyclic.title = 'Cycle sample with every recorded direction';
  cyclic.projection.layout = { mode: 'cyclic', reasons: ['Every recorded arrow is retained.'] };
  const source = path.join(workdir, 'cyclic.json'), output = path.join(workdir, 'cyclic.html');
  writeFileSync(source, JSON.stringify(cyclic));
  const run = runRenderer(source, output), receipt = run.stdout || {};
  const problems = [];
  if (run.status !== 0) problems.push(`exit ${run.status}: ${JSON.stringify(run.stderr)}`);
  if (receipt.representation?.status !== 'pass') problems.push('representation check did not pass');
  if (receipt.geometry?.status !== 'pass') problems.push('geometry check did not pass');
  if (receipt.nodes !== cyclic.projection.nodes.length || receipt.connections !== cyclic.projection.connections.length)
    problems.push('cycle rendering changed the graph counts');
  results.push({ check: 'render:cyclic', ok: problems.length === 0, problems, layout_mode: receipt.layout_mode,
    nodes: receipt.nodes, connections: receipt.connections });
  // Dense return edges exercise distinct rails, both directions between each
  // pair, and routes that pass other ranks and rows without crossing boxes.
  const template = cyclic.projection.connections[0];
  cyclic.projection.connections = cyclic.projection.nodes.flatMap((from) => cyclic.projection.nodes
    .filter((to) => to.id !== from.id).map((to) => ({ ...template, id: `dense_${from.id}_${to.id}`, from: from.id, to: to.id })));
  writeFileSync(source, JSON.stringify(cyclic));
  const denseRun = runRenderer(source, output), denseReceipt = denseRun.stdout || {};
  results.push({ check: 'render:cyclic-dense', ok: denseRun.status === 0 && denseReceipt.geometry?.status === 'pass'
    && denseReceipt.representation?.status === 'pass' && denseReceipt.connections === cyclic.projection.connections.length,
  diagnostics: denseRun.stderr, nodes: denseReceipt.nodes, connections: denseReceipt.connections });
}

export function main() {
  const results = [];
  const workdir = mkdtempSync(path.join(tmpdir(), 'paper-core-selftest-'));
  try {
    checkHashes(results);
    checkFixtures(results, workdir);
  } finally {
    rmSync(workdir, { recursive: true, force: true });
  }
  const ok = results.every((r) => r.ok);
  process.stdout.write(JSON.stringify({ ok, checks: results }, null, 1) + '\n');
  return ok ? 0 : 1;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main();
}
