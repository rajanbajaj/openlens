import assert from 'node:assert/strict';
import test from 'node:test';

import workflow from './workflow.js';

test('successful spilled result is a handoff, not "no usable result"', async () => {
  const ok = (ref) => ({
    ref,
    error_kind: null,
    data: { complete: true, evidence: [`${ref} evidence`], unresolved: [] },
    notes: '',
  });
  const spilled = {
    ref: 'run://spilled-1',
    error_kind: null,
    data: null,
    data_spilled_for_size: true,
    submitted_payload_bytes: 5000,
    submitted_payload_chars: 5000,
    notes: 'large notes',
  };
  let agentCalls = 0;
  const host = {
    args: {
      plan_path: 'p',
      context_path: 'c',
      waves: [
        {
          name: 'w',
          mode: 'parallel',
          agents: [{ label: 'a', owns: 'x', task: 't', done: 'd' }],
        },
      ],
      integrate: { label: 'integrate', owns: 'x', task: 't', done: 'd' },
    },
    parallel: async () => [spilled],
    agent: async () => ok(`agent-${++agentCalls}`),
  };

  const report = JSON.parse((await workflow(host)).summary);

  assert.equal(report.status, 'complete');
  assert.deepEqual(report.unresolved, []);
  assert.equal(report.waves[0].complete, true);
  assert.equal(report.waves[0].spilled, true);
  assert.equal(report.spilledResults[0].ref, 'run://spilled-1');
});
