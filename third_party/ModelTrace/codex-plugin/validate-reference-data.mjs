import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const rowsByFamily = Object.fromEntries(await Promise.all(['gpt', 'claude'].map(async (family) => {
  const referencePath = path.join(repository, 'data', `${family}_reference.jsonl`);
  const rows = (await readFile(referencePath, 'utf8'))
    .split(/\r?\n/u)
    .filter(Boolean)
    .map((line) => JSON.parse(line));
  return [family, rows];
})));

// These models were collected under the bounded-length reference policy. Raw
// provider output remains auditable; only parsed integers in [1, 355] enter the
// fingerprint, and gross count deviations must never reach a rebuilt bank.
const boundedModels = [
  { id: 'gpt-6-sol', family: 'gpt', provider: 'codex', reasoningEffort: 'low', cliVersion: '0.155.1' },
  { id: 'gpt-6-luna', family: 'gpt', provider: 'codex', reasoningEffort: 'low', cliVersion: '0.155.1' },
  { id: 'claude-opus-5-5', family: 'claude', provider: 'oaipro', responseModel: 'claude-opus-5-5', cleanRawNumbers: true },
];

function parseNumbers(text) {
  const runs = [];
  let current = [];
  let previousEnd = 0;
  for (const match of String(text).matchAll(/\d+/gu)) {
    const separator = String(text).slice(previousEnd, match.index);
    if (current.length && /\p{L}/u.test(separator)) {
      runs.push(current);
      current = [];
    }
    const value = Number(match[0]);
    if (value >= 1 && value <= 355) current.push(value);
    previousEnd = match.index + match[0].length;
  }
  if (current.length) runs.push(current);
  return runs.reduce((longest, run) => run.length > longest.length ? run : longest, []);
}

const summary = {};
for (const spec of boundedModels) {
  const selected = rowsByFamily[spec.family].filter((row) => row.source === spec.id);
  assert.equal(selected.length, 36, `${spec.id} must have 36 reference rows`);
  assert.equal(new Set(selected.map((row) => row.row_id)).size, 36, `${spec.id} row IDs must be unique`);
  assert.equal(new Set(selected.map((row) => row.challenge_id)).size, 36, `${spec.id} challenges must be unique`);
  const conditions = new Map();
  const outputHashes = new Set();
  let minimumRatio = Infinity;
  let maximumRatio = -Infinity;
  for (const row of selected) {
    assert.equal(row.strict_valid, true, `${row.row_id} is not strict-valid`);
    assert.equal(row.pattern_valid, true, `${row.row_id} contains a rejected rule pattern`);
    assert.equal(row.provider, spec.provider, `${row.row_id} provider changed`);
    if (spec.reasoningEffort) assert.equal(row.reasoning_effort, spec.reasoningEffort, `${row.row_id} reasoning effort changed`);
    if (spec.cliVersion) assert.equal(row.cli_version, spec.cliVersion, `${row.row_id} CLI version changed`);
    if (spec.responseModel) assert.equal(row.response_model, spec.responseModel, `${row.row_id} response model changed`);
    const numbers = parseNumbers(row.text);
    assert.equal(numbers.length, row.parsed_count, `${row.row_id} parsed_count is stale`);
    if (spec.cleanRawNumbers) {
      const raw = [...row.text.matchAll(/\d+/gu)].map((match) => Number(match[0]));
      assert.equal(raw.length, numbers.length, `${row.row_id} contains extraneous numbers`);
      assert.ok(raw.every((value) => value >= 1 && value <= 355), `${row.row_id} contains an out-of-range number`);
    }
    const ratio = numbers.length / row.requested_count;
    assert.ok(ratio >= 0.8 && ratio <= 1.2, `${row.row_id} count ratio ${ratio} is outside [0.8, 1.2]`);
    minimumRatio = Math.min(minimumRatio, ratio);
    maximumRatio = Math.max(maximumRatio, ratio);
    conditions.set(row.condition_id, (conditions.get(row.condition_id) ?? 0) + 1);
    const digest = createHash('sha256').update(row.text).digest('hex');
    assert.ok(!outputHashes.has(digest), `${row.row_id} duplicates another ${spec.id} output`);
    outputHashes.add(digest);
  }
  assert.equal(conditions.size, 12, `${spec.id} must cover 12 environments`);
  assert.deepEqual([...conditions.values()].sort((a, b) => a - b), Array(12).fill(3), `${spec.id} must have three rows per environment`);
  summary[spec.id] = { rows: selected.length, minimumRatio, maximumRatio };
}

process.stdout.write(JSON.stringify({ valid: true, policy: 'parsed/requested in [0.8, 1.2]', models: summary }) + '\n');
