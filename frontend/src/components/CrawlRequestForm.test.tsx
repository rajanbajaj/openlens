import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CrawlRequestForm } from './CrawlRequestForm.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function stubFetchOnce(status: number, payload: unknown) {
  const fetchStub = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
  vi.stubGlobal('fetch', fetchStub);
  return fetchStub;
}

async function submitUrl(url: string) {
  render(<CrawlRequestForm />);
  fireEvent.change(screen.getByLabelText('Page URL'), { target: { value: url } });
  // Submit the form directly: jsdom blocks implicit submission for
  // type="url" inputs holding invalid values, which is exactly the
  // 422 path under test.
  fireEvent.submit(screen.getByTestId('crawl-request-form'));
}

describe('CrawlRequestForm', () => {
  it('queues a URL and shows the pending confirmation', async () => {
    const fetchStub = stubFetchOnce(201, { status: 'pending', url: 'https://example.com/a' });
    await submitUrl('https://example.com/a');
    await waitFor(() => {
      expect(screen.getByTestId('crawl-request-notice')).toHaveTextContent('Queued for review');
    });
    expect(fetchStub).toHaveBeenCalledTimes(1);
    const [, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ url: 'https://example.com/a' });
  });

  it('explains duplicates without re-queueing', async () => {
    stubFetchOnce(200, { status: 'duplicate', url: 'https://example.com/a' });
    await submitUrl('https://example.com/a');
    await waitFor(() => {
      expect(screen.getByTestId('crawl-request-notice')).toHaveTextContent(
        'Already in the index queue',
      );
    });
  });

  it('reports validation and rate-limit failures inline', async () => {
    stubFetchOnce(422, { error: { code: 'invalid_request' } });
    await submitUrl('not-a-url');
    await waitFor(() => {
      expect(screen.getByTestId('crawl-request-notice')).toHaveTextContent('http(s)');
    });
    cleanup();
    stubFetchOnce(429, {});
    await submitUrl('https://example.com/b');
    await waitFor(() => {
      expect(screen.getAllByTestId('crawl-request-notice')[0]).toHaveTextContent(
        'Too many requests',
      );
    });
  });

  it('reports transport failures inline', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('down')));
    await submitUrl('https://example.com/c');
    await waitFor(() => {
      expect(screen.getByTestId('crawl-request-notice')).toHaveTextContent('Could not reach');
    });
  });
});
