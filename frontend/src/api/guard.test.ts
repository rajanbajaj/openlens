import { describe, expect, it } from 'vitest';
import { readAgentTrace, readChatResponse, readChatTrace, readPerspectives } from './guard.ts';

const TRACE = {
  rewritten_query: 'cheap bluetooth headphones',
  filters: { max_price: 50 },
  steps: [
    { kind: 'rewrite', detail: 'expanded with bluetooth' },
    { kind: 'retrieve', detail: 'queried 3 providers' },
  ],
  fallbacks: ['provider mock timed out, used cache'],
  model_calls: 2,
};

function envelope(trace: unknown) {
  return { metadata: { agent_trace: trace } };
}

describe('readAgentTrace', () => {
  it('narrows a full trace', () => {
    expect(readAgentTrace(envelope(TRACE))).toEqual(TRACE);
  });

  it('returns null when metadata or the trace is absent', () => {
    expect(readAgentTrace({})).toBeNull();
    expect(readAgentTrace({ metadata: {} })).toBeNull();
    expect(readAgentTrace({ metadata: null })).toBeNull();
    expect(readAgentTrace(null)).toBeNull();
    expect(readAgentTrace('trace')).toBeNull();
  });

  it('returns null for empty or fully malformed traces', () => {
    expect(readAgentTrace(envelope({}))).toBeNull();
    expect(readAgentTrace(envelope({ steps: [], fallbacks: [] }))).toBeNull();
    expect(readAgentTrace(envelope('trace'))).toBeNull();
    expect(readAgentTrace(envelope(42))).toBeNull();
  });

  it('keeps partial traces with only the renderable fields', () => {
    expect(readAgentTrace(envelope({ rewritten_query: 'headphones', model_calls: 0 }))).toEqual({
      rewritten_query: 'headphones',
      model_calls: 0,
    });
  });

  it('drops malformed steps and fallbacks instead of crashing', () => {
    expect(
      readAgentTrace(
        envelope({
          steps: [{ kind: 'retrieve' }, { detail: 'no kind' }, {}, 42, 'nope', null],
          fallbacks: ['ok', 42, '', null],
          model_calls: 'two',
        }),
      ),
    ).toEqual({
      steps: [{ kind: 'retrieve' }, { detail: 'no kind' }],
      fallbacks: ['ok'],
    });
  });

  it('ignores malformed scalars and non-record filters', () => {
    expect(
      readAgentTrace(
        envelope({
          rewritten_query: '',
          filters: ['max_price'],
          model_calls: -1,
        }),
      ),
    ).toBeNull();
    expect(readAgentTrace(envelope({ model_calls: 2.5 }))).toBeNull();
  });
});

describe('readChatTrace model label', () => {
  it('keeps the serving-model label for attribution', () => {
    expect(readChatTrace({ rewritten_query: 'q', model: 'test-model-1' })).toEqual({
      rewritten_query: 'q',
      model: 'test-model-1',
    });
  });

  it('drops blank or non-string model values', () => {
    expect(readChatTrace({ rewritten_query: 'q', model: '' })).toEqual({
      rewritten_query: 'q',
    });
    expect(readChatTrace({ rewritten_query: 'q', model: 42 })).toEqual({
      rewritten_query: 'q',
    });
  });

  it('survives the chat response narrowing used by persisted transcripts', () => {
    const response = readChatResponse({
      answer: 'hi',
      citations: [],
      results: [],
      trace: { rewritten_query: 'q', model_calls: 1, model: 'test-model-1' },
      notice: null,
    });
    expect(response?.trace.model).toBe('test-model-1');
  });
});

describe('readPerspectives', () => {
  const VIEW = {
    story_id: 's1',
    claim: 'The bridge reopened',
    representative_id: 'a',
    member_ids: ['a', 'b'],
    stances: [
      { source_id: 'a', stance: 'supports', evidence: 'Reopened Monday.' },
      { source_id: 'b', stance: 'bogus', evidence: 42 },
    ],
    supports: 1,
    refutes: 0,
    neutral: 1,
    multi_source: true,
    missing_note: 'no refuting source in this cluster',
    trust_caveats: ['b: low credibility signal (0.20)'],
  };

  it('narrows a full view, degrading unknown stances to neutral', () => {
    const [view] = readPerspectives({ perspectives: [VIEW] });
    expect(view.claim).toBe('The bridge reopened');
    expect(view.member_ids).toEqual(['a', 'b']);
    expect(view.stances?.[0]).toEqual({
      source_id: 'a',
      stance: 'supports',
      evidence: 'Reopened Monday.',
    });
    expect(view.stances?.[1]?.stance).toBe('neutral');
    expect(view.missing_note).toBe('no refuting source in this cluster');
    expect(view.trust_caveats).toEqual(['b: low credibility signal (0.20)']);
  });

  it('returns [] for absent, malformed, or content-free blocks', () => {
    expect(readPerspectives({})).toEqual([]);
    expect(readPerspectives({ perspectives: 'nope' })).toEqual([]);
    expect(readPerspectives({ perspectives: [{ stances: [{ stance: 'supports' }] }] })).toEqual([]);
    expect(readPerspectives(null)).toEqual([]);
  });

  it('carries perspectives through chat response narrowing', () => {
    const response = readChatResponse({
      answer: 'hi [1]',
      citations: [{ ref: 1, title: 'T', url: null }],
      results: [],
      trace: {},
      notice: null,
      perspectives: [VIEW],
    });
    expect(response?.perspectives?.[0]?.claim).toBe('The bridge reopened');
  });
});
