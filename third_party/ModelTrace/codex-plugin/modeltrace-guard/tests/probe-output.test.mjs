import test from 'node:test';
import assert from 'node:assert/strict';
import { ProbeOutputError, validateNumbers } from '../scripts/probe-output.mjs';
import { FORK_ROLE_MARKER, LANGUAGES, forkPrompt } from '../scripts/prompts.mjs';

// Synthetic parser fixtures only; no model or identification evaluation.
const values = Array.from({ length: 300 }, (_, i) => (i * 37 + 11) % 355 + 1);
const array = JSON.stringify(values);
const fence = (body, tag = 'json', marker = '```', newline = '\n') => marker + tag + newline + body + newline + marker;

test('bare arrays and a single JSON/unlabeled code fence preserve every number and its order', () => {
  for (const text of [array, ' \n' + array + '\n ', fence(array), fence(array, ''),
    fence(array, 'JSON', '```', '\r\n'), fence(array, 'json', '````'), fence(array, 'json', '~~~'),
    '\n ' + fence(JSON.stringify(values, null, 1)) + '\n', '``` json\n' + array + '\n  ```']) {
    assert.deepEqual(validateNumbers(text, 300), values);
  }
});

test('wrapping does not relax the existing natural-count, integer or range restrictions', () => {
  const shorter = values.slice(0, 200);
  assert.deepEqual(validateNumbers(fence(JSON.stringify(shorter)), 300), shorter);
  for (const body of ['[0,' + values.join(',') + ']', '[356,' + values.join(',') + ']',
    '[-1,' + values.join(',') + ']', '[1.5,' + values.join(',') + ']', '[1e2,' + values.join(',') + ']',
    '["12",' + values.join(',') + ']', '[true,' + values.join(',') + ']', '[01,' + values.join(',') + ']',
    '[1+1,' + values.join(',') + ']', JSON.stringify(values.slice(0, 164)), JSON.stringify([...values, ...values.slice(0, 76)])]) {
    assert.throws(() => validateNumbers(body, 300), ProbeOutputError);
    assert.throws(() => validateNumbers(fence(body), 300), ProbeOutputError);
  }
});

test('prose, multiple arrays, nested blocks, incomplete fences and non-JSON labels stay rejected', () => {
  for (const text of ['Here are the integers:\n' + array, array + '\nDone.',
    'Answer:\n' + fence(array), fence(array) + '\nDone.', array + '\n' + array,
    fence(array) + '\n' + fence(array), fence(array + '\n' + array), fence(fence(array)),
    '```json\n' + array, '```json\n' + array + '\n~~~', fence(array, 'javascript'),
    fence(array, 'jsonc'), fence('// comment\n' + array), JSON.stringify({ numbers: values })]) {
    assert.throws(() => validateNumbers(text, 300), ProbeOutputError);
  }
});

test('output errors expose stable diagnostic types without reply fragments or numeric contents', () => {
  const secret = 'private task content that must not appear in diagnostics';
  const cases = [
    [null, 'missing_text', 'none'], ['', 'empty_answer', 'none'], ['x'.repeat(5001), 'output_too_long', 'none'],
    [secret + array, 'invalid_json', 'bare'], [fence(secret + array), 'invalid_json', 'code_fence'],
    ['```json\n' + array, 'invalid_code_fence', 'code_fence'], ['{}', 'not_array', 'bare'],
    ['["' + secret + '"]', 'non_literal_integer', 'bare'],
    [fence('[356,' + values.join(',') + ']'), 'out_of_range', 'code_fence'],
    ['[1,2,3]', 'count_out_of_bounds', 'bare'], ['[]', 'count_out_of_bounds', 'bare'],
  ];
  for (const [text, code, format] of cases) {
    assert.throws(() => validateNumbers(text, 300), (error) => {
      assert.ok(error instanceof ProbeOutputError);
      assert.equal(error.code, 'MODELTRACE_PROBE_OUTPUT');
      assert.equal(error.diagnostic.code, code);
      assert.equal(error.diagnostic.format, format);
      assert.equal(error.diagnostic.textLength, text?.length ?? null);
      assert.equal(error.diagnostic.expectedCount, 300);
      const serialized = error.message + JSON.stringify(error);
      assert.ok(!serialized.includes(secret)); assert.ok(!serialized.includes(array));
      assert.ok(Object.keys(error.diagnostic).every((key) => ['code', 'format', 'textLength', 'expectedCount', 'receivedCount'].includes(key)));
      return true;
    });
  }
});

test('each language uses a stable sampling role and does not inject a session ID or earlier answer', () => {
  for (const language of LANGUAGES) {
    const first = forkPrompt(language, 300), second = forkPrompt(language, 301);
    const lines = first.split('\n');
    assert.equal(lines.length, 4);
    assert.equal(lines[0], FORK_ROLE_MARKER);
    assert.ok(lines[1].includes('/goal'));
    assert.ok(lines[2].includes('300')); assert.ok(second.split('\n')[2].includes('301'));
    assert.deepEqual(second.split('\n').slice(0, 2), lines.slice(0, 2));
    assert.equal(first, forkPrompt(language, 300));
    assert.ok(!first.includes(array));
  }
});
