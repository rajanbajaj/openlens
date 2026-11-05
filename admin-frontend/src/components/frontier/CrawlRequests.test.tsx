import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AdminQueryProvider } from '../../api/queryClient.tsx';
import { CrawlRequests } from './CrawlRequests.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function makeRequests(count: number, status = 'pending') {
  return {
    requests: Array.from({ length: count }, (_, i) => ({
      id: 100 + i,
      url: `https://example.com/user-page-${i}`,
      status,
      created_at: '2026-11-01T00:00:00Z',
      decided_at: null,
    })),
    total: count,
  };
}

function stubBackend() {
  const seen: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string, init?: { method?: string }) => {
      const text = String(url);
      seen.push(text);
      let payload: unknown = makeRequests(25);
      const status = new URL(text, 'http://localhost').searchParams.get('status');
      if (text.includes('/api/v1/frontier/crawl-requests/') && text.endsWith('/approve')) {
        const id = Number(text.split('/crawl-requests/')[1].split('/')[0]);
        payload = {
          id,
          url: `https://example.com/user-page-${id - 100}`,
          status: 'approved',
          created_at: '2026-11-01T00:00:00Z',
          decided_at: '2026-11-02T00:00:00Z',
        };
      } else if (
        text.includes('/api/v1/frontier/crawl-requests') &&
        !text.includes('/approve') &&
        !text.includes('/decline')
      ) {
        payload = makeRequests(25, status ?? 'pending');
      }
      void init;
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers(),
        text: () => Promise.resolve(JSON.stringify(payload)),
        json: () => Promise.resolve(payload),
      });
    }),
  );
  return seen;
}

function renderRequests() {
  return render(
    <AdminQueryProvider>
      <CrawlRequests adminToken="secret" onChanged={vi.fn()} />
    </AdminQueryProvider>,
  );
}

describe('CrawlRequests', () => {
  it('filters by status tab through the backend param', async () => {
    const seen = stubBackend();
    renderRequests();
    await waitFor(() => {
      expect(screen.getByTestId('frontier-crawl-requests')).toHaveTextContent('user-page-0');
    });
    fireEvent.click(screen.getByTestId('crawl-requests-tab-approved'));
    await waitFor(() => {
      expect(seen.some((url) => url.includes('status=approved'))).toBe(true);
    });
  });

  it('pages client-side over the fetched list', async () => {
    stubBackend();
    renderRequests();
    await waitFor(() => {
      expect(screen.getByTestId('frontier-crawl-requests')).toHaveTextContent('user-page-0');
    });
    expect(screen.getByTestId('frontier-crawl-requests-page')).toHaveTextContent('Page 1');
    fireEvent.click(screen.getByTestId('frontier-crawl-requests-next'));
    await waitFor(() => {
      expect(screen.getByTestId('frontier-crawl-requests-page')).toHaveTextContent('Page 2');
    });
    // Lexicographic URL sort: page 2 holds user-page-5…9.
    expect(screen.getByTestId('frontier-crawl-requests')).toHaveTextContent('user-page-9');
    expect(screen.getByTestId('frontier-crawl-requests')).not.toHaveTextContent('user-page-0');
  });

  it('confirms a single approve through the dialog', async () => {
    stubBackend();
    renderRequests();
    await waitFor(() => {
      expect(screen.getByTestId('crawl-request-approve-100')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('crawl-request-approve-100'));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Approve 1 request(s)?');
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent(/Approved/);
    });
  });

  it('bulk-declines the selection behind one confirm', async () => {
    stubBackend();
    renderRequests();
    await waitFor(() => {
      expect(screen.getByTestId('frontier-crawl-requests')).toHaveTextContent('user-page-0');
    });
    fireEvent.click(screen.getByTestId('frontier-crawl-requests-select-all'));
    await waitFor(() => {
      expect(screen.getByTestId('crawl-requests-bulk-decline')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('crawl-requests-bulk-decline'));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Decline 20 request(s)?');
    fireEvent.click(screen.getByTestId('confirm-dialog-cancel'));
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });
});
