// Accept presentation-only wrapping, never extract numbers from prose or repair
// a sample. Diagnostics deliberately contain no reply snippets or numeric values.
export class ProbeOutputError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'ProbeOutputError';
    this.code = 'MODELTRACE_PROBE_OUTPUT';
    this.diagnostic = { ...details, code };
  }
}

export function validateNumbers(text, expectedCount) {
  const diagnostic = { format: 'none', textLength: typeof text === 'string' ? text.length : null, expectedCount };
  const fail = (code, message) => { throw new ProbeOutputError(code, message, diagnostic); };
  if (typeof text !== 'string') fail('missing_text', 'Probe did not return a text answer');
  if (text.length > 5000) fail('output_too_long', 'Probe answer exceeds the 5000-character limit');
  let body = text.trim();
  if (!body) fail('empty_answer', 'Probe returned an empty answer');
  diagnostic.format = 'bare';
  if (/^(?:`{3}|~{3})/.test(body)) {
    diagnostic.format = 'code_fence';
    // One complete unlabeled/JSON block only; matching fence and no outer prose.
    const block = /^(`{3,}|~{3,})[ \t]*(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*\1$/i.exec(body);
    if (!block) fail('invalid_code_fence', 'Probe answer must be one complete JSON code block with no surrounding text');
    body = block[2].trim();
  }
  let values;
  try { values = JSON.parse(body); }
  catch { fail('invalid_json', 'Probe answer is not one valid JSON array; prose, multiple blocks and incomplete JSON are not accepted'); }
  if (!Array.isArray(values)) fail('not_array', 'Probe answer must be a JSON array, not another JSON value');
  diagnostic.receivedCount = values.length;
  if (values.length && !/^\[\s*\d+(?:\s*,\s*\d+)*\s*\]$/.test(body)) {
    fail('non_literal_integer', 'Probe numbers must be literal integers, without signs, decimals, exponents or expressions');
  }
  if (values.some((value) => !Number.isInteger(value) || value < 1 || value > 355)) fail('out_of_range', 'Every number must be in 1..355');
  // Preserve natural counting errors and the existing bank-compatible cutoff.
  if (values.length < Math.max(80, Math.ceil(expectedCount * 0.55)) || values.length > Math.ceil(expectedCount * 1.25)) {
    fail('count_out_of_bounds', `Sample length ${values.length} is outside the accepted bounds for ${expectedCount}`);
  }
  return values;
}
