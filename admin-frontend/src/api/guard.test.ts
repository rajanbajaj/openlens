import { describe, expect, it } from 'vitest';
import {
  isAnalyticsCounts,
  isAnalyticsSeries,
  isAnalyticsTop,
  isCrawlRequest,
  isCrawlRequestList,
  isFeedbackSummary,
  isFrontierSettingsResponse,
  isFrontierStatus,
  readFrontierDiscovery,
  readFrontierNewUrls,
  readFrontierQueue,
  readFrontierSettings,
  readFrontierWorkers,
} from './guard.ts';

describe('admin guards', () => {
  it('accepts a well-formed frontier status and rejects junk', () => {
    const status = {
      paused: false,
      queue: [{ url: 'https://example.com/a', depth: 0, status: 'queued' }],
      queue_depth: 1,
      checkpoint: { phase: 'running', pages_attempted: 1, pages_indexed: 1 },
      new_urls: [],
    };
    expect(isFrontierStatus(status)).toBe(true);
    expect(isFrontierStatus({ ...status, paused: 'no' })).toBe(false);
    expect(isFrontierStatus(null)).toBe(false);
  });

  it('reads queue, workers, discovery, and settings leniently', () => {
    const status = {
      queue: [{ url: 'https://example.com/a', depth: 0, status: 'queued' }, 'junk'],
      new_urls: [{ url: 'https://example.com/b', status: 'new' }],
      workers: [
        {
          worker_id: 'w-1',
          crawl_id: 'default',
          last_seen: 1,
          claimed: 2,
          status: 'alive',
        },
        'junk',
      ],
      discovery: { at: 1, feeds_checked: 2, new_urls: 3, refreshed: 4 },
    };
    expect(readFrontierQueue(status)).toHaveLength(1);
    expect(readFrontierNewUrls(status)).toHaveLength(1);
    expect(readFrontierWorkers(status)).toHaveLength(1);
    expect(readFrontierDiscovery(status)).toMatchObject({ feeds_checked: 2 });
    expect(readFrontierDiscovery({})).toBeNull();
    const settings = {
      settings: {
        max_pages: 100,
        max_queued_urls: 1000,
        crawl_delay_s: 0.5,
        default_max_depth: 3,
        respect_robots: true,
        user_agent: 'TestBot',
        skip_domains: [],
        allow_domains: [],
        deny_domains: [],
        discovery_feeds: [],
        refresh_after_days: 30,
        discovery_interval_s: 21600,
      },
    };
    expect(isFrontierSettingsResponse(settings)).toBe(true);
    expect(readFrontierSettings(settings)?.max_pages).toBe(100);
    expect(readFrontierSettings({})).toBeNull();
  });

  it('validates crawl-request review shapes', () => {
    const row = {
      id: 7,
      url: 'https://example.com/u',
      status: 'pending',
      created_at: '2026-11-01T00:00:00Z',
      decided_at: null,
    };
    expect(isCrawlRequest(row)).toBe(true);
    expect(isCrawlRequest({ ...row, status: 'queued' })).toBe(false);
    expect(isCrawlRequestList({ requests: [row], total: 1 })).toBe(true);
    expect(isCrawlRequestList({ requests: [], total: 0 })).toBe(true);
    expect(isCrawlRequestList({ requests: ['junk'], total: 1 })).toBe(false);
  });

  it('validates analytics and feedback summary shapes', () => {
    expect(isAnalyticsCounts({ days: 30, total: 1, by_name: { a: 1 } })).toBe(true);
    expect(isAnalyticsCounts({ days: 30, total: 1 })).toBe(false);
    expect(
      isAnalyticsTop({ name: 'crawl.page', prop: 'domain', days: 30, total: 1, top: [] }),
    ).toBe(true);
    expect(isAnalyticsSeries({ name: 'crawl.page', bucket: 'day', days: 30, points: [] })).toBe(
      true,
    );
    expect(isFeedbackSummary({ total: 0, by_action: {}, by_reason: {}, top_docs: [] })).toBe(true);
    expect(isFeedbackSummary({ total: 0 })).toBe(false);
  });
});
