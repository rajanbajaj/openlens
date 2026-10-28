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
  assert.deepEqual(report.record.attempted, false);
});

const baseArgs = () => ({
  plan_path: 'p',
  context_path: 'c',
  waves: [
    {
      name: 'w',
      mode: 'solo',
      agents: [{ label: 'a', owns: 'x', task: 't', done: 'd' }],
    },
  ],
  integrate: { label: 'integrate', owns: 'x', task: 't', done: 'd' },
});

const okAgent = (evidence) => ({
  ref: 'run://ok',
  error_kind: null,
  data: { complete: true, evidence, unresolved: [] },
  notes: '',
});

test('record stage skipped without status_update; run verdict unchanged', async () => {
  const calls = [];
  const host = {
    args: baseArgs(),
    parallel: async () => { throw new Error('no parallel waves in this spec'); },
    agent: async (req) => { calls.push(req.label); return okAgent([`${req.label} evidence`]); },
  };

  const report = JSON.parse((await workflow(host)).summary);

  assert.deepEqual(calls, ['a', 'integrate', 'synthesis']);
  assert.equal(report.status, 'complete');
  assert.deepEqual(report.record, { attempted: false, complete: false, evidence: [], unresolved: [] });
});

test('record stage runs last with the status_update payload', async () => {
  const calls = [];
  const host = {
    args: {
      ...baseArgs(),
      status_update: { row: 5, status: 'Done', notes: 'recorded by the run', session_slug: '2026-10-28-1900_task-5' },
    },
    parallel: async () => { throw new Error('no parallel waves in this spec'); },
    agent: async (req) => { calls.push(req); return okAgent([`${req.label} evidence`]); },
  };

  const report = JSON.parse((await workflow(host)).summary);

  assert.deepEqual(calls.map((c) => c.label), ['a', 'integrate', 'synthesis', 'record']);
  const recordInput = calls[3].input;
  assert.match(recordInput, /row 5/);
  assert.match(recordInput, /"Done"/);
  assert.match(recordInput, /2026-10-28-1900_task-5/);
  assert.match(recordInput, /tasks_status\.md/);
  assert.equal(report.status, 'complete');
  assert.equal(report.record.attempted, true);
  assert.equal(report.record.complete, true);
  assert.deepEqual(report.record.evidence, ['record evidence']);
});

test('failed record is reported without flipping the run verdict', async () => {
  const host = {
    args: { ...baseArgs(), status_update: { row: 5, status: 'Done' } },
    parallel: async () => { throw new Error('no parallel waves in this spec'); },
    agent: async (req) => req.label === 'record'
      ? { ref: null, error_kind: 'agent_failed', data: null, notes: '' }
      : okAgent([`${req.label} evidence`]),
  };

  const report = JSON.parse((await workflow(host)).summary);

  assert.equal(report.status, 'complete');
  assert.equal(report.record.attempted, true);
  assert.equal(report.record.complete, false);
  assert.ok(report.record.unresolved.length > 0);
});
