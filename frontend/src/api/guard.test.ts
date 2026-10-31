import { describe, expect, it } from 'vitest';
import { readAgentTrace, readChatResponse, readChatTrace } from './guard.ts';

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
