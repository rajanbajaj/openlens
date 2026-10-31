import { describe, expect, it } from 'vitest';
import { buildChatBody, clampChatLimit, validateChatMessage } from './chat.ts';
import { isChatResponse, readChatResponse } from './guard.ts';

const HIT = {
  id: 'r1',
  title: 'Price page',
  snippet: 'Costs $9 today',
  original_score: 0.9,
  final_score: 0.95,
  url: 'https://example.com/x',
  provider: 'mock',
  reasons: [],
  warnings: [],
  metadata: {},
};

const CHAT_OK = {
  answer: 'It costs [1] today.',
  citations: [{ ref: 1, title: 'Price page', url: 'https://example.com/x' }],
  results: [HIT],
  trace: {
    rewritten_query: 'price today?',
    filters: {},
    steps: [{ kind: 'retrieve', detail: 'queried mock' }],
    fallbacks: [],
    model_calls: 1,
  },
  notice: null,
};

describe('validateChatMessage', () => {
  it('rejects blank messages and over-long messages', () => {
    expect(validateChatMessage('')).not.toBeNull();
    expect(validateChatMessage('   ')).not.toBeNull();
    expect(validateChatMessage('x'.repeat(1001))).not.toBeNull();
  });

  it('accepts a 1-1000 char message', () => {
    expect(validateChatMessage('hello?')).toBeNull();
    expect(validateChatMessage('x'.repeat(1000))).toBeNull();
  });
});

describe('clampChatLimit', () => {
  it('defaults non-finite and out-of-range limits to 5', () => {
    expect(clampChatLimit(undefined)).toBe(5);
    expect(clampChatLimit(Number.NaN)).toBe(5);
    expect(clampChatLimit(0)).toBe(5);
    expect(clampChatLimit(11)).toBe(5);
  });

  it('keeps in-range limits, flooring fractions', () => {
    expect(clampChatLimit(1)).toBe(1);
    expect(clampChatLimit(10)).toBe(10);
    expect(clampChatLimit(3.9)).toBe(3);
  });
});

describe('buildChatBody', () => {
  it('applies the default limit and empty history', () => {
    expect(buildChatBody({ message: 'hi' })).toEqual({ message: 'hi', history: [], limit: 5 });
  });

  it('caps history at 10 turns, oldest dropped first', () => {
    const history = Array.from({ length: 12 }, (_, i) => ({
      role: (i % 2 === 0 ? 'user' : 'assistant') as 'user' | 'assistant',
      content: `turn ${i}`,
    }));
    const body = buildChatBody({ message: 'latest', history });
    expect(body.history).toHaveLength(10);
    expect(body.history[0]).toEqual({ role: 'user', content: 'turn 2' });
    expect(body.history[9]).toEqual({ role: 'assistant', content: 'turn 11' });
  });

  it('drops malformed history turns instead of sending them', () => {
    const body = buildChatBody({
      message: 'hi',
      history: [
        { role: 'user', content: 'ok' },
        { role: 'system', content: 'nope' } as unknown as { role: 'user'; content: string },
        { role: 'user', content: '' },
      ],
    });
    expect(body.history).toEqual([{ role: 'user', content: 'ok' }]);
  });
});

describe('isChatResponse', () => {
  it('accepts a full contract envelope', () => {
    expect(isChatResponse(CHAT_OK)).toBe(true);
  });

  it('accepts a fail-closed notice envelope with empty answer', () => {
    expect(
      isChatResponse({ answer: '', citations: [], results: [HIT], trace: {}, notice: 'off' }),
    ).toBe(true);
  });

  it('rejects malformed envelopes', () => {
    expect(isChatResponse(null)).toBe(false);
    expect(isChatResponse({})).toBe(false);
    expect(isChatResponse({ ...CHAT_OK, answer: 42 })).toBe(false);
    expect(isChatResponse({ ...CHAT_OK, citations: [{ ref: 0, title: 'x', url: null }] })).toBe(
      false,
    );
    expect(isChatResponse({ ...CHAT_OK, results: [{ id: 'bad' }] })).toBe(false);
    expect(isChatResponse({ ...CHAT_OK, notice: 42 })).toBe(false);
    expect(isChatResponse({ ...CHAT_OK, trace: 'trace' })).toBe(false);
  });
});

describe('readChatResponse', () => {
  it('narrows a full envelope and keeps the trace', () => {
    // Empty `fallbacks` degrade to absent (null-safe render skips them).
    const { trace: _trace, ...rest } = CHAT_OK;
    void _trace;
    expect(readChatResponse(CHAT_OK)).toEqual({
      ...rest,
      trace: {
        rewritten_query: 'price today?',
        filters: {},
        steps: [{ kind: 'retrieve', detail: 'queried mock' }],
        model_calls: 1,
      },
    });
  });

  it('degrades absent notice/trace to null-safe defaults', () => {
    const { notice: _n, trace: _t, ...rest } = CHAT_OK as unknown as Record<string, unknown>;
    void _n;
    void _t;
    expect(readChatResponse(rest)).toEqual({
      ...(rest as object),
      notice: null,
      trace: {},
    });
  });

  it('drops malformed citations and returns null for bad results', () => {
    const narrowed = readChatResponse({
      ...CHAT_OK,
      citations: [{ ref: 1, title: 'ok', url: null }, { ref: 0, title: 'bad' }, 'nope'],
    });
    expect(narrowed?.citations).toEqual([{ ref: 1, title: 'ok', url: null }]);
    expect(readChatResponse({ ...CHAT_OK, results: [{ id: 'bad' }] })).toBeNull();
    expect(readChatResponse(null)).toBeNull();
  });
});
