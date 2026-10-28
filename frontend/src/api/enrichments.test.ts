import { describe, expect, it } from 'vitest';
import {
  resultAiDigest,
  resultAlsoFrom,
  resultCommerce,
  resultSignals,
  resultStoryId,
  resultUnderstanding,
  summarizePluginExecution,
} from './enrichments.ts';

const RICH_METADATA: Record<string, unknown> = {
  signals: { trust: 0.55, relevance: 1, bogus: 'high', freshness: 0.5 },
  story_id: 'story-2',
  also_from: ['opensearch', 42],
  price: 199.99,
  currency: 'USD',
  location: 'Berlin',
  facilities: ['wifi', 'pool'],
  published: '2026-10-27',
  ai_summary: 'Two headphones compared.',
  ai_perspectives: ['Audiophiles prefer wired.'],
  ai_intent: 'shopping',
  ai_entities: ['headphones'],
  ai_model: 'rule-based',
};

describe('enrichment readers', () => {
  it('reads display-ordered finite signals, skipping malformed values', () => {
    expect(resultSignals(RICH_METADATA)).toEqual([
      ['relevance', 1],
      ['trust', 0.55],
      ['freshness', 0.5],
    ]);
  });

  it('reads story grouping, federation attribution, and commerce attributes', () => {
    expect(resultStoryId(RICH_METADATA)).toBe('story-2');
    expect(resultAlsoFrom(RICH_METADATA)).toEqual(['opensearch']);
    expect(resultCommerce(RICH_METADATA)).toEqual({
      price: 199.99,
      currency: 'USD',
      location: 'Berlin',
      facilities: ['wifi', 'pool'],
      published: '2026-10-27',
    });
  });

  it('reads the labelled AI digest and understanding, absent when disabled', () => {
    expect(resultAiDigest(RICH_METADATA)).toEqual({
      summary: 'Two headphones compared.',
      perspectives: ['Audiophiles prefer wired.'],
      model: 'rule-based',
    });
    expect(resultUnderstanding(RICH_METADATA)).toEqual({
      intent: 'shopping',
      entities: ['headphones'],
      model: 'rule-based',
    });
    expect(resultAiDigest({})).toBeNull();
    expect(resultUnderstanding({})).toBeNull();
  });

  it('degrades gracefully on absent or malformed metadata', () => {
    expect(resultSignals({})).toEqual([]);
    expect(resultSignals({ signals: 'high' })).toEqual([]);
    expect(resultStoryId({ story_id: 7 })).toBeNull();
    expect(resultAlsoFrom({})).toEqual([]);
    expect(resultCommerce({ price: 'free', facilities: 'wifi' })).toEqual({
      price: null,
      currency: null,
      location: null,
      facilities: ['wifi'],
      published: null,
    });
  });

  it('summarizes plugin executions with unknown-tolerant fallbacks', () => {
    expect(
      summarizePluginExecution({
        plugin: 'safety-filter',
        stage: 'filtering',
        status: 'ok',
        latency_ms: 0.4,
        error: null,
      }),
    ).toEqual({
      plugin: 'safety-filter',
      stage: 'filtering',
      status: 'ok',
      latencyMs: 0.4,
      error: null,
    });
    expect(summarizePluginExecution({})).toEqual({
      plugin: 'unknown',
      stage: 'unknown',
      status: 'unknown',
      latencyMs: null,
      error: null,
    });
  });
});
