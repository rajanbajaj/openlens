import { describe, expect, it, vi } from 'vitest';
import { MAX_QUERY_LENGTH, buildSearchBody, debounce, sanitizeInput } from './client.ts';

describe('sanitizeInput', () => {
  it('trims surrounding whitespace', () => {
    expect(sanitizeInput('  hello world  ')).toBe('hello world');
  });

  it('strips ASCII control characters but keeps tab, LF, and CR', () => {
    const raw =
      'a' +
      String.fromCharCode(0) +
      'b' +
      String.fromCharCode(7) +
      'c' +
      String.fromCharCode(31) +
      'd' +
      String.fromCharCode(127) +
      'e\tf\ng\rh';
    expect(sanitizeInput(raw)).toBe('abcde\tf\ng\rh');
  });

  it('leaves query operators and interior spacing alone', () => {
    expect(sanitizeInput('  site:example.com "exact phrase" -excluded  ')).toBe(
      'site:example.com "exact phrase" -excluded',
    );
  });

  it('caps the length at the limit', () => {
    expect(sanitizeInput('x'.repeat(MAX_QUERY_LENGTH + 10))).toHaveLength(MAX_QUERY_LENGTH);
    expect(sanitizeInput('abcdef', 3)).toBe('abc');
  });

  it('resolves blank or control-only input to empty', () => {
    expect(sanitizeInput('   ')).toBe('');
    expect(sanitizeInput(String.fromCharCode(0, 7, 9, 10, 13, 31))).toBe('');
  });
});

describe('buildSearchBody sanitization', () => {
  it('sanitizes the query at the request boundary', () => {
    const body = buildSearchBody({ query: `  hello${String.fromCharCode(7)}  ` });
    expect(body.query).toBe('hello');
  });
});

describe('debounce', () => {
  it('coalesces rapid calls into one call with the latest arguments', () => {
    vi.useFakeTimers();
    try {
      const fn = vi.fn();
      const debounced = debounce(fn, 100);
      debounced('a');
      debounced('b');
      debounced('c');
      expect(fn).not.toHaveBeenCalled();
      vi.advanceTimersByTime(100);
      expect(fn).toHaveBeenCalledTimes(1);
      expect(fn).toHaveBeenCalledWith('c');
    } finally {
      vi.useRealTimers();
    }
  });

  it('drops the pending call when cancelled', () => {
    vi.useFakeTimers();
    try {
      const fn = vi.fn();
      const debounced = debounce(fn, 100);
      debounced('a');
      debounced.cancel();
      vi.advanceTimersByTime(500);
      expect(fn).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
