import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../web/observability.js', import.meta.url), 'utf8');

for (const [search, expected] of [['?evidence=live', 'live'], ['', 'experiments'], ['?evidence=unexpected', 'experiments']]) {
  test(`Evidence deep link ${search || '(default)'}`, () => {
    const result = runInNewContext(source + '\nobservation.source;', {
      URLSearchParams, location: { search }, state: { view: 'investigation' },
      window: { addEventListener() {} },
    });
    assert.equal(result, expected);
  });
}
