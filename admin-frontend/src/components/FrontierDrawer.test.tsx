import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FrontierDrawer } from './FrontierDrawer.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const STATUS = {
  paused: false,
  queue: [{ url: 'https://example.com/a', depth: 0, status: 'queued', priority: 2 }],
  queue_depth: 1,
  checkpoint: { phase: 'running', pages_attempted: 1, pages_indexed: 1 },
  new_urls: [],
};

const SETTINGS = {
  max_pages: 100,
  max_queued_urls: 1000,
  crawl_delay_s: 0.5,
  default_max_depth: 3,
  respect_robots: true,
  user_agent: 'TestBot/1.0 (+https://example.com)',
  skip_domains: [],
  allow_domains: [],
  deny_domains: [],
  discovery_feeds: [],
  refresh_after_days: 30.0,
  discovery_interval_s: 21600.0,
};

/** Minimal socket stand-in: the drawer only needs onopen/onmessage/close. */
class MockSocket {
  static instances: MockSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  url: string;
  protocols: string[] = [];
  closed = false;
  constructor(url: string, protocols: string[] = []) {
    this.url = url;
    this.protocols = protocols;
    MockSocket.instances.push(this);
  }
  close(): void {
    this.closed = true;
  }
  open(): void {
    this.onopen?.();
  }
  push(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }
}

function stubFetch(handler: (url: string) => unknown): void {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation((url: string) =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers(),
        text: () => Promise.resolve(JSON.stringify(handler(url))),
        json: () => Promise.resolve(handler(url)),
      }),
    ),
  );
}

function stubBackend(statusPayload: unknown = STATUS): void {
  stubFetch((url: string) => {
    if (url.includes('/api/v1/frontier/settings')) return { settings: SETTINGS };
    if (url.includes('/api/v1/frontier/workers')) return { workers: [], worker_count: 0 };
    return statusPayload;
  });
}

describe('FrontierDrawer live stats and submission feedback', () => {
  it('connects to the live socket and applies pushed status', async () => {
    MockSocket.instances = [];
    stubBackend();
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="secret" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-queue')).toHaveTextContent('example.com/a');
    });
    expect(MockSocket.instances).toHaveLength(1);
    expect(MockSocket.instances[0].url).toBe('ws://localhost:8000/api/v1/frontier/live');
    expect(MockSocket.instances[0].protocols).toEqual(['x-admin-token', 'secret']);

    MockSocket.instances[0].open();
    await waitFor(() => {
      expect(screen.getByTestId('frontier-live')).toHaveTextContent('Live');
    });

    const pushed = {
      ...STATUS,
      queue: [{ url: 'https://example.com/b', depth: 1, status: 'queued', priority: 5 }],
    };
    MockSocket.instances[0].push(pushed);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-queue')).toHaveTextContent('example.com/b');
    });
  });

  it('explains a disabled kafka queue instead of unknown lag', async () => {
    stubBackend({ ...STATUS });
    render(<FrontierDrawer adminToken="" />);
    expect(await screen.findByTestId('frontier-kafka-lag')).toHaveTextContent('store-backed queue');
  });

  it('shows the lag count when kafka is enabled', async () => {
    stubBackend({
      ...STATUS,
      kafka_enabled: true,
      kafka_lag_total: 12,
      kafka_lag_partitions: 3,
    });
    render(<FrontierDrawer adminToken="" />);
    expect(await screen.findByTestId('frontier-kafka-lag')).toHaveTextContent('12 urls');
  });

  it('keeps the last lag probe across live frames that skip it', async () => {
    MockSocket.instances = [];
    stubBackend({
      ...STATUS,
      kafka_enabled: true,
      kafka_lag_total: 12,
      kafka_lag_partitions: 3,
    });
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    expect(await screen.findByTestId('frontier-kafka-lag')).toHaveTextContent('12 urls');
    MockSocket.instances[0].open();
    MockSocket.instances[0].push({ ...STATUS, kafka_enabled: true });
    await waitFor(() => {
      expect(screen.getByTestId('frontier-queue')).toBeInTheDocument();
    });
    expect(screen.getByTestId('frontier-kafka-lag')).toHaveTextContent('12 urls');
  });

  it('ignores malformed live frames without breaking the dashboard', async () => {
    MockSocket.instances = [];
    stubBackend();
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-queue')).toHaveTextContent('example.com/a');
    });
    MockSocket.instances[0].open();
    MockSocket.instances[0].onmessage?.({ data: 'not json{{' });
    expect(screen.getByTestId('frontier-queue')).toHaveTextContent('example.com/a');
    expect(screen.getByTestId('frontier-state')).toBeInTheDocument();
  });

  it('pauses and clears the queue behind a confirm dialog', async () => {
    MockSocket.instances = [];
    stubBackend({ ...STATUS, cleared_queued: 2, kafka_purged: true });
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByText('Pause & clear')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('Pause & clear'));
    expect(screen.getByRole('alertdialog')).toHaveTextContent(
      'Pause crawling and clear the queue?',
    );
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent(
        'Paused. Cleared 2 queued URL(s). Topic purged.',
      );
    });
  });

  it('does nothing when the clear confirm is dismissed', async () => {
    MockSocket.instances = [];
    let clearCalls = 0;
    stubFetch((url: string) => {
      if (url.includes('/api/v1/frontier/clear')) clearCalls += 1;
      if (url.includes('/api/v1/frontier/settings')) return { settings: SETTINGS };
      if (url.includes('/api/v1/frontier/workers')) return { workers: [], worker_count: 0 };
      return STATUS;
    });
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByText('Pause & clear')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('Pause & clear'));
    fireEvent.click(screen.getByTestId('confirm-dialog-cancel'));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(clearCalls).toBe(0);
  });

  it('confirms a successful seed resume with a seed count', async () => {
    MockSocket.instances = [];
    // Resume arms only while paused; the running fleet keeps it disabled.
    stubBackend({ ...STATUS, paused: true });
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-seeds')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId('frontier-seeds'), {
      target: { value: 'https://example.com/new\n' },
    });
    fireEvent.click(screen.getByText('Resume'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent('Resumed with 1 seed(s).');
    });
  });

  it('submits seeds without resuming the crawl', async () => {
    MockSocket.instances = [];
    let seedsCalls = 0;
    let resumeCalls = 0;
    stubFetch((url: string) => {
      if (url.includes('/api/v1/frontier/seeds')) {
        seedsCalls += 1;
        return { enqueued: 2 };
      }
      if (url.includes('/api/v1/frontier/resume')) resumeCalls += 1;
      if (url.includes('/api/v1/frontier/settings')) return { settings: SETTINGS };
      if (url.includes('/api/v1/frontier/workers')) return { workers: [], worker_count: 0 };
      return STATUS;
    });
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-seeds-submit')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId('frontier-seeds'), {
      target: { value: 'https://example.com/a\nhttps://example.com/b\n' },
    });
    fireEvent.click(screen.getByTestId('frontier-seeds-submit'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent(
        'Enqueued 2 seed(s) — crawlers pick them up without interruption.',
      );
    });
    expect(seedsCalls).toBe(1);
    expect(resumeCalls).toBe(0);
  });

  it('shows how many seeds the queue cap skipped', async () => {
    MockSocket.instances = [];
    stubFetch((url: string) => {
      if (url.includes('/api/v1/frontier/seeds')) {
        return { enqueued: 1, skipped_over_cap: 2 };
      }
      if (url.includes('/api/v1/frontier/settings')) return { settings: SETTINGS };
      if (url.includes('/api/v1/frontier/workers')) return { workers: [], worker_count: 0 };
      return STATUS;
    });
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-seeds-submit')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId('frontier-seeds'), {
      target: { value: 'https://example.com/a\nhttps://example.com/b\nhttps://example.com/c\n' },
    });
    fireEvent.click(screen.getByTestId('frontier-seeds-submit'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent(
        'Enqueued 1 seed(s) — crawlers pick them up without interruption. 2 skipped: queue is at the Max queued URLs cap.',
      );
    });
  });

  it('submits seeds with priority and depth', async () => {
    MockSocket.instances = [];
    let seedsBody: unknown = null;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string, init?: { body?: string }) => {
        if (url.includes('/api/v1/frontier/seeds') && init?.body) {
          seedsBody = JSON.parse(init.body);
        }
        const payload = url.includes('/api/v1/frontier/seeds')
          ? { enqueued: 1 }
          : url.includes('/api/v1/frontier/settings')
            ? { settings: SETTINGS }
            : url.includes('/api/v1/frontier/workers')
              ? { workers: [], worker_count: 0 }
              : STATUS;
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: new Headers(),
          text: () => Promise.resolve(JSON.stringify(payload)),
          json: () => Promise.resolve(payload),
        });
      }),
    );
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-seeds-submit')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId('frontier-seeds'), {
      target: { value: 'https://example.com/a\n' },
    });
    fireEvent.change(screen.getByTestId('frontier-priority'), {
      target: { value: '3' },
    });
    fireEvent.change(screen.getByTestId('frontier-depth'), {
      target: { value: '2' },
    });
    fireEvent.click(screen.getByTestId('frontier-seeds-submit'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent('Enqueued 1 seed(s)');
    });
    expect(seedsBody).toEqual({
      seeds: ['https://example.com/a'],
      priority: 3,
      depth: 2,
    });
  });

  it('refreshes the domain boards on demand', async () => {
    MockSocket.instances = [];
    let boardCalls = 0;
    stubFetch((url: string) => {
      if (url.includes('/api/v1/frontier/failed/domains') && !url.includes('/urls')) {
        boardCalls += 1;
      }
      if (url.includes('/api/v1/frontier/failed/urls')) return { urls: [], total: 0 };
      if (url.includes('/api/v1/frontier/failed/domains')) {
        return { domains: [{ domain: 'example.com', failed: 1 }], total: 1 };
      }
      if (url.includes('/api/v1/frontier/queued/domains')) {
        return { domains: [], total: 0 };
      }
      if (url.includes('/api/v1/frontier/settings')) return { settings: SETTINGS };
      if (url.includes('/api/v1/frontier/workers')) return { workers: [], worker_count: 0 };
      return STATUS;
    });
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-domains-refresh')).toBeInTheDocument();
    });
    expect(boardCalls).toBe(1);
    fireEvent.click(screen.getByTestId('frontier-domains-refresh'));
    await waitFor(() => {
      expect(boardCalls).toBeGreaterThan(1);
    });
  });

  it('auto-refreshes the domain boards every minute', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      MockSocket.instances = [];
      let boardCalls = 0;
      stubFetch((url: string) => {
        if (url.includes('/api/v1/frontier/failed/domains') && !url.includes('/urls')) {
          boardCalls += 1;
        }
        if (url.includes('/api/v1/frontier/failed/urls')) return { urls: [], total: 0 };
        if (url.includes('/api/v1/frontier/failed/domains')) {
          return { domains: [{ domain: 'example.com', failed: 1 }], total: 1 };
        }
        if (url.includes('/api/v1/frontier/queued/domains')) {
          return { domains: [], total: 0 };
        }
        if (url.includes('/api/v1/frontier/settings')) return { settings: SETTINGS };
        if (url.includes('/api/v1/frontier/workers')) return { workers: [], worker_count: 0 };
        return STATUS;
      });
      vi.stubGlobal('WebSocket', MockSocket);
      render(<FrontierDrawer adminToken="" />);
      await waitFor(() => {
        expect(screen.getByTestId('frontier-domains-refresh')).toBeInTheDocument();
      });
      expect(boardCalls).toBe(1);
      await act(async () => {
        vi.advanceTimersByTime(60000);
      });
      await waitFor(() => {
        expect(boardCalls).toBeGreaterThan(1);
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it('refuses a bad seed depth inline', async () => {
    MockSocket.instances = [];
    let seedsCalls = 0;
    stubFetch((url: string) => {
      if (url.includes('/api/v1/frontier/seeds')) seedsCalls += 1;
      if (url.includes('/api/v1/frontier/settings')) return { settings: SETTINGS };
      if (url.includes('/api/v1/frontier/workers')) return { workers: [], worker_count: 0 };
      return STATUS;
    });
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-seeds-submit')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId('frontier-seeds'), {
      target: { value: 'https://example.com/a\n' },
    });
    fireEvent.change(screen.getByTestId('frontier-depth'), {
      target: { value: '-1' },
    });
    fireEvent.click(screen.getByTestId('frontier-seeds-submit'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent('Depth must be a whole number.');
    });
    expect(seedsCalls).toBe(0);
  });

  it('refuses to submit with no seeds', async () => {
    MockSocket.instances = [];
    let seedsCalls = 0;
    stubFetch((url: string) => {
      if (url.includes('/api/v1/frontier/seeds')) seedsCalls += 1;
      if (url.includes('/api/v1/frontier/settings')) return { settings: SETTINGS };
      if (url.includes('/api/v1/frontier/workers')) return { workers: [], worker_count: 0 };
      return STATUS;
    });
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-seeds-submit')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('frontier-seeds-submit'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent('Add at least one seed URL first.');
    });
    expect(seedsCalls).toBe(0);
  });
});

describe('FrontierDrawer settings form', () => {
  it('disables Pause while paused and shows the state hint', async () => {
    MockSocket.instances = [];
    stubBackend({ ...STATUS, paused: true });
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    const pause = await screen.findByText('Pause', { selector: 'button' });
    expect(pause).toBeDisabled();
    expect(screen.getByText('Resume', { selector: 'button' })).not.toBeDisabled();
    expect(screen.getByTestId('frontier-pause-hint')).toHaveTextContent(
      'Paused — queue growth halted',
    );
    expect(screen.getByTestId('frontier-state')).toHaveTextContent('Paused');
  });

  it('shows an error panel with retry instead of defaults when the snapshot fails', async () => {
    MockSocket.instances = [];
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('backend down')));
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    expect(await screen.findByTestId('frontier-snapshot-error')).toHaveTextContent(
      'Frontier status unavailable.',
    );
    expect(screen.queryByTestId('frontier-settings')).not.toBeInTheDocument();
  });

  it('reverts dirty settings instead of saving them', async () => {
    MockSocket.instances = [];
    stubBackend();
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    const input = (await screen.findByTestId('frontier-setting-user-agent')) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'OtherBot/2.0 (+https://example.com)' } });
    expect(screen.getAllByText('Unsaved changes').length).toBeGreaterThan(0);
    expect(screen.getByTestId('frontier-settings-revert')).not.toBeDisabled();
    fireEvent.click(screen.getByTestId('frontier-settings-revert'));
    expect((screen.getByTestId('frontier-setting-user-agent') as HTMLInputElement).value).toBe(
      'TestBot/1.0 (+https://example.com)',
    );
  });

  it('flags a non-integer seed priority inline instead of toast-only', async () => {
    MockSocket.instances = [];
    stubBackend();
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-seeds-submit')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId('frontier-priority'), { target: { value: '1.5' } });
    fireEvent.click(screen.getByTestId('frontier-seeds-submit'));
    await waitFor(() => {
      expect(screen.getByTestId('frontier-priority-error')).toHaveTextContent(
        'Priority must be a whole number (0 or more).',
      );
    });
    expect(screen.getByTestId('frontier-priority')).toHaveAttribute('aria-invalid', 'true');
  });

  it('shows the loaded User-Agent and saves edits back', async () => {
    MockSocket.instances = [];
    let putBody: unknown = null;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string, init?: { body?: string }) => {
        if (url.includes('/api/v1/frontier/settings') && init?.body) {
          putBody = JSON.parse(init.body);
        }
        // The generated client reads `.text()`: mirror the per-URL
        // payload there, not just in `.json()`.
        const payload = url.includes('/api/v1/frontier/settings')
          ? { settings: SETTINGS }
          : url.includes('/api/v1/frontier/workers')
            ? { workers: [], worker_count: 0 }
            : STATUS;
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: new Headers(),
          text: () => Promise.resolve(JSON.stringify(payload)),
          json: () => Promise.resolve(payload),
        });
      }),
    );
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    const input = (await screen.findByTestId('frontier-setting-user-agent')) as HTMLInputElement;
    expect(input.value).toBe('TestBot/1.0 (+https://example.com)');
    fireEvent.change(input, { target: { value: 'OtherBot/2.0 (+https://example.com)' } });
    fireEvent.click(screen.getByText('Save settings'));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Push settings to fleet?');
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent('Settings saved.');
    });
    expect(putBody).toMatchObject({ user_agent: 'OtherBot/2.0 (+https://example.com)' });
  });
});

describe('FrontierDrawer skip domains', () => {
  function stubFetchCapture(capture: { body: unknown }): void {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string, init?: { body?: string }) => {
        if (url.includes('/api/v1/frontier/settings') && init?.body) {
          capture.body = JSON.parse(init.body);
        }
        // The generated client reads `.text()`: mirror the per-URL
        // payload there, not just in `.json()`.
        const payload = url.includes('/api/v1/frontier/settings')
          ? { settings: SETTINGS }
          : url.includes('/api/v1/frontier/workers')
            ? { workers: [], worker_count: 0 }
            : { ...STATUS };
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: new Headers(),
          text: () => Promise.resolve(JSON.stringify(payload)),
          json: () => Promise.resolve(payload),
        });
      }),
    );
  }

  it('adds, dedupes, removes, and saves skip domains', async () => {
    MockSocket.instances = [];
    const capture: { body: unknown } = { body: null };
    stubFetchCapture(capture);
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    const input = await screen.findByTestId('frontier-skip-input');
    expect(screen.queryByTestId('frontier-skip-list')).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: 'https://Ads.Example.com/feed' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(screen.getByTestId('frontier-skip-list')).toHaveTextContent('ads.example.com');

    fireEvent.change(input, { target: { value: 'ads.example.com' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent('already skipped');
    });

    fireEvent.click(screen.getByTestId('frontier-skip-remove-ads.example.com'));
    expect(screen.queryByTestId('frontier-skip-list')).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: 'tracker.example' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.click(screen.getByText('Save settings'));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Push settings to fleet?');
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent('Settings saved.');
    });
    expect(capture.body).toMatchObject({ skip_domains: ['tracker.example'] });
  });

  it('adds and saves allow and block domains', async () => {
    MockSocket.instances = [];
    const capture: { body: unknown } = { body: null };
    stubFetchCapture(capture);
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    const allowInput = await screen.findByTestId('frontier-allow-input');
    const denyInput = await screen.findByTestId('frontier-deny-input');
    expect(screen.queryByTestId('frontier-allow-list')).not.toBeInTheDocument();
    expect(screen.queryByTestId('frontier-deny-list')).not.toBeInTheDocument();

    fireEvent.change(allowInput, { target: { value: 'https://News.Example.com/top' } });
    fireEvent.keyDown(allowInput, { key: 'Enter' });
    expect(screen.getByTestId('frontier-allow-list')).toHaveTextContent('news.example.com');

    fireEvent.change(denyInput, { target: { value: 'ads.example' } });
    fireEvent.keyDown(denyInput, { key: 'Enter' });
    expect(screen.getByTestId('frontier-deny-list')).toHaveTextContent('ads.example');

    fireEvent.click(screen.getByText('Save settings'));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Push settings to fleet?');
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent('Settings saved.');
    });
    expect(capture.body).toMatchObject({
      allow_domains: ['news.example.com'],
      deny_domains: ['ads.example'],
    });
  });

  it('adds and saves discovery feeds', async () => {
    MockSocket.instances = [];
    const capture: { body: unknown } = { body: null };
    stubFetchCapture(capture);
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    const feedInput = await screen.findByTestId('frontier-feed-input');
    expect(screen.queryByTestId('frontier-feed-list')).not.toBeInTheDocument();

    fireEvent.change(feedInput, { target: { value: 'example.com/feed.xml' } });
    fireEvent.keyDown(feedInput, { key: 'Enter' });
    expect(screen.getByTestId('frontier-feed-list')).toHaveTextContent(
      'https://example.com/feed.xml',
    );

    fireEvent.click(screen.getByText('Save settings'));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Push settings to fleet?');
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent('Settings saved.');
    });
    expect(capture.body).toMatchObject({ discovery_feeds: ['https://example.com/feed.xml'] });
  });

  it('shows the skipped count in the queue header', async () => {
    MockSocket.instances = [];
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) =>
        Promise.resolve({
          ok: true,
          status: 200,
          headers: new Headers(),
          text: () =>
            Promise.resolve(
              JSON.stringify(
                url.includes('/api/v1/frontier/settings')
                  ? { settings: SETTINGS }
                  : {
                      ...STATUS,
                      queue: [
                        { url: 'https://a.example/x', depth: 0, status: 'queued', priority: 0 },
                        { url: 'https://b.example/y', depth: 1, status: 'skipped', priority: 0 },
                        { url: 'https://c.example/z', depth: 1, status: 'skipped', priority: 0 },
                      ],
                      queue_depth: 3,
                    },
              ),
            ),
          json: () =>
            Promise.resolve(
              url.includes('/api/v1/frontier/settings')
                ? { settings: SETTINGS }
                : {
                    ...STATUS,
                    queue: [
                      { url: 'https://a.example/x', depth: 0, status: 'queued', priority: 0 },
                      { url: 'https://b.example/y', depth: 1, status: 'skipped', priority: 0 },
                      { url: 'https://c.example/z', depth: 1, status: 'skipped', priority: 0 },
                    ],
                    queue_depth: 3,
                  },
            ),
        }),
      ),
    );
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByText(/2\s+skipped/)).toBeInTheDocument();
    });
  });

  it('wraps long queue URLs so narrow screens do not scroll sideways', async () => {
    MockSocket.instances = [];
    const longUrl = `https://example.com/${'path-segment-'.repeat(20)}?a=1&b=2`;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) =>
        Promise.resolve({
          ok: true,
          status: 200,
          headers: new Headers(),
          text: () =>
            Promise.resolve(
              JSON.stringify(
                url.includes('/api/v1/frontier/settings')
                  ? { settings: SETTINGS }
                  : {
                      ...STATUS,
                      queue: [{ url: longUrl, depth: 0, status: 'queued', priority: 0 }],
                      queue_depth: 1,
                    },
              ),
            ),
          json: () =>
            Promise.resolve(
              url.includes('/api/v1/frontier/settings')
                ? { settings: SETTINGS }
                : {
                    ...STATUS,
                    queue: [{ url: longUrl, depth: 0, status: 'queued', priority: 0 }],
                    queue_depth: 1,
                  },
            ),
        }),
      ),
    );
    vi.stubGlobal('WebSocket', MockSocket);
    const { container } = render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-queue')).toBeInTheDocument();
    });
    const row = screen.getByTestId('frontier-queue').querySelector('li');
    expect(row?.className).toMatch(/break-all/);
    expect(container.querySelector('.break-all')).toHaveTextContent(longUrl.slice(0, 32));
  });

  it('pages through failed URLs with reasons', async () => {
    const failed = Array.from({ length: 25 }, (_, i) => ({
      url: `https://example.com/f${i}`,
      reason: i % 2 ? 'fetch timeout' : 'denied by deny_domains',
      attempts: 3,
    }));
    stubFetch((url: string) => {
      if (url.includes('/api/v1/frontier/settings')) return { settings: SETTINGS };
      if (url.includes('/api/v1/frontier/workers')) return { workers: [], worker_count: 0 };
      if (url.includes('/api/v1/frontier/failed')) {
        const params = new URL(url, 'http://localhost').searchParams;
        const limit = Number(params.get('limit') ?? 20);
        const offset = Number(params.get('offset') ?? 0);
        return {
          urls: failed.slice(offset, offset + limit),
          total: failed.length,
          limit,
          offset,
        };
      }
      return STATUS;
    });
    render(<FrontierDrawer adminToken="" />);
    expect(await screen.findByTestId('frontier-failed')).toBeInTheDocument();
    expect(screen.getByTestId('frontier-failed-page')).toHaveTextContent('Page 1 of 2');
    expect(screen.getByTestId('frontier-failed')).toHaveTextContent('fetch timeout');
    expect(screen.getByTestId('frontier-failed-prev')).toBeDisabled();
    fireEvent.click(screen.getByTestId('frontier-failed-next'));
    await waitFor(() => {
      expect(screen.getByTestId('frontier-failed-page')).toHaveTextContent('Page 2 of 2');
    });
    expect(screen.getByTestId('frontier-failed')).toHaveTextContent('https://example.com/f24');
    expect(screen.getByTestId('frontier-failed-next')).toBeDisabled();
    fireEvent.click(screen.getByTestId('frontier-failed-prev'));
    await waitFor(() => {
      expect(screen.getByTestId('frontier-failed-page')).toHaveTextContent('Page 1 of 2');
    });
  });

  it('shows an empty state when nothing failed', async () => {
    stubFetch((url: string) => {
      if (url.includes('/api/v1/frontier/settings')) return { settings: SETTINGS };
      if (url.includes('/api/v1/frontier/workers')) return { workers: [], worker_count: 0 };
      if (url.includes('/api/v1/frontier/failed')) {
        return { urls: [], total: 0, limit: 20, offset: 0 };
      }
      return STATUS;
    });
    render(<FrontierDrawer adminToken="" />);
    expect(await screen.findByTestId('frontier-failed-empty')).toBeInTheDocument();
  });

  it('jumps to a page number in failed URLs', async () => {
    const failed = Array.from({ length: 45 }, (_, i) => ({
      url: `https://example.com/f${i}`,
      reason: 'fetch timeout',
      attempts: 3,
    }));
    stubFetch((url: string) => {
      if (url.includes('/api/v1/frontier/settings')) return { settings: SETTINGS };
      if (url.includes('/api/v1/frontier/workers')) return { workers: [], worker_count: 0 };
      if (url.includes('/api/v1/frontier/failed')) {
        const params = new URL(url, 'http://localhost').searchParams;
        const limit = Number(params.get('limit') ?? 20);
        const offset = Number(params.get('offset') ?? 0);
        return {
          urls: failed.slice(offset, offset + limit),
          total: failed.length,
          limit,
          offset,
        };
      }
      return STATUS;
    });
    render(<FrontierDrawer adminToken="" />);
    expect(await screen.findByTestId('frontier-failed')).toBeInTheDocument();
    expect(screen.getByTestId('frontier-failed-page')).toHaveTextContent('Page 1 of 3');
    fireEvent.change(screen.getByTestId('frontier-failed-jump'), {
      target: { value: '3' },
    });
    fireEvent.click(screen.getByTestId('frontier-failed-go'));
    await waitFor(() => {
      expect(screen.getByTestId('frontier-failed-page')).toHaveTextContent('Page 3 of 3');
    });
    expect(screen.getByTestId('frontier-failed')).toHaveTextContent('https://example.com/f44');
    expect(screen.getByTestId('frontier-failed-next')).toBeDisabled();
  });
});

describe('FrontierDrawer crawl-request review', () => {
  const REQUESTS = {
    requests: [
      {
        id: 7,
        url: 'https://example.com/user-page',
        status: 'pending',
        created_at: '2026-11-01T00:00:00Z',
        decided_at: null,
      },
    ],
    total: 1,
  };

  function stubReviewBackend() {
    stubFetch((url: string) => {
      if (url.includes('/api/v1/frontier/settings')) return { settings: SETTINGS };
      if (url.includes('/api/v1/frontier/workers')) return { workers: [], worker_count: 0 };
      if (url.includes('/api/v1/frontier/crawl-requests/7/approve')) {
        return { ...REQUESTS.requests[0], status: 'approved' };
      }
      if (url.includes('/api/v1/frontier/crawl-requests/7/decline')) {
        return { ...REQUESTS.requests[0], status: 'declined' };
      }
      if (url.includes('/api/v1/frontier/crawl-requests')) return REQUESTS;
      return STATUS;
    });
    vi.stubGlobal('WebSocket', MockSocket);
  }

  it('lists pending requests and toasts on approve', async () => {
    stubReviewBackend();
    render(<FrontierDrawer adminToken="secret" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-crawl-requests')).toHaveTextContent(
        'example.com/user-page',
      );
    });
    fireEvent.click(screen.getByTestId('crawl-request-approve-7'));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Approve 1 request(s)?');
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent(/Approved/);
    });
  });

  it('toasts on decline', async () => {
    stubReviewBackend();
    render(<FrontierDrawer adminToken="secret" />);
    await waitFor(() => {
      expect(screen.getByTestId('crawl-request-decline-7')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('crawl-request-decline-7'));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Decline 1 request(s)?');
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent(/Declined/);
    });
  });

  it('shows the empty state when nothing awaits review', async () => {
    stubFetch((url: string) => {
      if (url.includes('/api/v1/frontier/settings')) return { settings: SETTINGS };
      if (url.includes('/api/v1/frontier/workers')) return { workers: [], worker_count: 0 };
      if (url.includes('/api/v1/frontier/crawl-requests')) {
        return { requests: [], total: 0 };
      }
      return STATUS;
    });
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="secret" />);
    await waitFor(() => {
      expect(screen.getByText('No URLs awaiting review')).toBeInTheDocument();
    });
  });
});

describe('FrontierDrawer worker throughput', () => {
  it('renders cluster and per-worker throughput from stats fragments', async () => {
    MockSocket.instances = [];
    stubFetch((url: string) => {
      if (url.includes('/api/v1/frontier/settings')) return { settings: SETTINGS };
      if (url.includes('/api/v1/frontier/workers'))
        return {
          workers: [
            {
              worker_id: 'w-1',
              crawl_id: 'default',
              last_seen: 60,
              claimed: 2,
              status: 'alive',
              stats: { at: 60, pages: 22, tokens: 1100, errors: 0 },
              pages_per_min: 12,
              tokens_per_min: 600,
            },
            {
              worker_id: 'w-2',
              crawl_id: 'default',
              last_seen: 60,
              claimed: 0,
              status: 'alive',
            },
          ],
          worker_count: 2,
        };
      return STATUS;
    });
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="secret" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-throughput')).toHaveTextContent('12/min');
    });
    expect(screen.getByTestId('frontier-throughput')).toHaveTextContent('22 pages');
    // Per-worker row carries rates and cumulative totals; the stat-less
    // worker renders its base row without throughput.
    expect(screen.getByTestId('frontier-workers')).toHaveTextContent('22 pages · 1100 tokens');
    expect(screen.getByTestId('frontier-workers')).toHaveTextContent('w-2');
  });
});

describe('FrontierDrawer failed domains', () => {
  const DOMAINS = {
    domains: [
      { domain: 'example.com', failed: 12, top_reason: 'fetch timeout' },
      { domain: 'other.example', failed: 3, top_reason: 'http 404' },
    ],
    total: 2,
    limit: 20,
    offset: 0,
  };
  const QUEUED = {
    domains: [
      { domain: 'example.com', queued: 7, top_priority: 2 },
      { domain: 'other.example', queued: 2, top_priority: 0 },
    ],
    total: 2,
    limit: 20,
    offset: 0,
  };

  function stubDomainBackend(capture: {
    posts: { url: string; body: unknown }[];
    puts: { url: string; body: unknown }[];
  }): void {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string, init?: { method?: string; body?: string }) => {
        const text = String(url);
        const method = init?.method ?? 'GET';
        let payload: unknown = STATUS;
        if (text.includes('/api/v1/frontier/settings') && method === 'PUT') {
          capture.puts.push({ url: text, body: init?.body });
          payload = { settings: { ...SETTINGS, ...JSON.parse(String(init?.body ?? '{}')) } };
        } else if (text.includes('/api/v1/frontier/settings')) payload = { settings: SETTINGS };
        else if (text.includes('/api/v1/frontier/workers'))
          payload = { workers: [], worker_count: 0 };
        else if (text.includes('/api/v1/frontier/failed/domains/example.com/urls'))
          payload = {
            urls: [
              { url: 'https://example.com/a', reason: 'fetch timeout', attempts: 3 },
              { url: 'https://example.com/b', reason: 'http 404', attempts: 2 },
            ],
            total: 2,
            limit: 20,
            offset: 0,
          };
        else if (
          text.includes('/api/v1/frontier/failed/domains/auto-triage') &&
          method === 'POST'
        ) {
          capture.posts.push({ url: text, body: init?.body });
          payload = {
            triaged: [
              {
                domain: 'example.com',
                top_reason: 'http 404',
                action: 'skip+purge',
                affected: 12,
                wait_s: 0,
              },
              {
                domain: 'other.example',
                top_reason: 'http 429',
                action: 'requeue+wait',
                affected: 3,
                wait_s: 180,
              },
            ],
            dry_run: false,
          };
        } else if (text.includes('/api/v1/frontier/failed/domains')) payload = DOMAINS;
        else if (text.includes('/api/v1/frontier/failed')) payload = { urls: [], total: 0 };
        else if (text.includes('/api/v1/frontier/queued/domains/example.com/urls'))
          payload = {
            urls: [
              { url: 'https://example.com/q1', reason: '', attempts: 0 },
              { url: 'https://example.com/q2', reason: '', attempts: 0 },
            ],
            total: 2,
            limit: 20,
            offset: 0,
          };
        else if (text.includes('/api/v1/frontier/queued/domains')) payload = QUEUED;
        else if (text.includes('/api/v1/frontier/domains/requeue') && method === 'POST') {
          capture.posts.push({ url: text, body: init?.body });
          payload = { domains: ['example.com'], affected: 12 };
        } else if (text.includes('/api/v1/frontier/domains/skip') && method === 'POST') {
          capture.posts.push({ url: text, body: init?.body });
          payload = { domains: ['example.com'], affected: 1 };
        } else if (text.includes('/api/v1/frontier/domains/priority') && method === 'POST') {
          capture.posts.push({ url: text, body: init?.body });
          payload = { domains: ['example.com'], affected: 5 };
        } else if (text.includes('/api/v1/admin/config/indexing') && method === 'PUT') {
          capture.puts.push({ url: text, body: init?.body });
          payload = {
            doc: 'indexing',
            body: { deny_domains: ['blocked.example', 'example.com'] },
            version: 2,
            updated_at: null,
            source: 'db',
          };
        } else if (text.includes('/api/v1/admin/config/indexing')) {
          payload = {
            doc: 'indexing',
            body: { deny_domains: ['blocked.example'] },
            version: 1,
            updated_at: null,
            source: 'file',
          };
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: new Headers(),
          text: () => Promise.resolve(JSON.stringify(payload)),
          json: () => Promise.resolve(payload),
        });
      }),
    );
  }

  it('bulk-requeues selected failed domains behind one confirm', async () => {
    MockSocket.instances = [];
    const capture = {
      posts: [] as { url: string; body: unknown }[],
      puts: [] as { url: string; body: unknown }[],
    };
    stubDomainBackend(capture);
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="secret" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-failed-domains')).toHaveTextContent('example.com');
    });
    fireEvent.click(screen.getByTestId('frontier-failed-domains-select-all'));
    fireEvent.click(screen.getByTestId('frontier-failed-domains-bulk-requeue'));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Requeue 2 domain(s)?');
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent(
        'Requeued — example.com, other.example: 12 row(s).',
      );
    });
    expect(JSON.parse(capture.posts[0].body as string)).toMatchObject({
      domains: ['example.com', 'other.example'],
    });
  });

  it('renders failed domains with counts and action buttons', async () => {
    MockSocket.instances = [];
    const empty: {
      posts: { url: string; body: unknown }[];
      puts: { url: string; body: unknown }[];
    } = {
      posts: [],
      puts: [],
    };
    stubDomainBackend(empty);
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-failed-domains')).toHaveTextContent('example.com');
    });
    expect(screen.getByTestId('frontier-failed-domains')).toHaveTextContent('12');
    expect(screen.getByTestId('frontier-failed-domains')).toHaveTextContent('fetch timeout');
    expect(screen.getByTestId('frontier-domain-requeue-example.com')).toBeInTheDocument();
    expect(screen.getByTestId('frontier-domain-flush-example.com')).toBeInTheDocument();
    expect(screen.getByTestId('frontier-domain-skip-example.com')).toBeInTheDocument();
    expect(screen.getByTestId('frontier-domain-block-example.com')).toBeInTheDocument();
  });

  it('requeues a domain and toasts the affected count', async () => {
    MockSocket.instances = [];
    const capture = {
      posts: [] as { url: string; body: unknown }[],
      puts: [] as { url: string; body: unknown }[],
    };
    stubDomainBackend(capture);
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="secret" />);
    fireEvent.click(await screen.findByTestId('frontier-domain-requeue-example.com'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent('Requeued — example.com: 12 row(s).');
    });
    expect(capture.posts).toHaveLength(1);
    expect(capture.posts[0].url).toContain('/api/v1/frontier/domains/requeue');
  });

  it('blocks a domain by merging deny_domains behind a confirm', async () => {
    MockSocket.instances = [];
    const capture = { posts: [], puts: [] as { url: string; body: unknown }[] };
    stubDomainBackend(capture);
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="secret" />);
    fireEvent.click(await screen.findByTestId('frontier-domain-block-example.com'));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Block example.com?');
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent(
        'Blocked — example.com added to deny_domains.',
      );
    });
    expect(capture.puts).toHaveLength(2);
    expect(capture.puts[0].url).toContain('/api/v1/admin/config/indexing');
    expect(JSON.parse(capture.puts[0].body as string)).toMatchObject({
      body: { deny_domains: ['blocked.example', 'example.com'] },
    });
    expect(capture.puts[1].url).toContain('/api/v1/frontier/settings');
    expect(JSON.parse(capture.puts[1].body as string)).toMatchObject({
      deny_domains: expect.arrayContaining(['example.com']),
    });
    expect(screen.getByTestId('frontier-deny-list')).toHaveTextContent('example.com');
  });

  it('runs auto-triage behind a confirm and toasts the action mix', async () => {
    MockSocket.instances = [];
    const capture = {
      posts: [] as { url: string; body: unknown }[],
      puts: [] as { url: string; body: unknown }[],
    };
    stubDomainBackend(capture);
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="secret" />);
    fireEvent.click(await screen.findByTestId('frontier-auto-triage'));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Auto-triage every failed domain?');
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent(
        'Auto-triage done — 2 domain(s): 1 skip+purge, 1 requeue+wait.',
      );
    });
    expect(capture.posts).toHaveLength(1);
    expect(capture.posts[0].url).toContain('/api/v1/frontier/failed/domains/auto-triage');
  });

  it('renders queued domains with priority, skip, and block buttons', async () => {
    MockSocket.instances = [];
    const empty: {
      posts: { url: string; body: unknown }[];
      puts: { url: string; body: unknown }[];
    } = {
      posts: [],
      puts: [],
    };
    stubDomainBackend(empty);
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-queued-domains')).toHaveTextContent('example.com');
    });
    expect(screen.getByTestId('frontier-queued-domains')).toHaveTextContent('7');
    expect(screen.getByTestId('frontier-queued-domains')).toHaveTextContent('p2');
    expect(screen.getByTestId('frontier-queued-prio-up-example.com')).toBeInTheDocument();
    expect(screen.getByTestId('frontier-queued-prio-down-example.com')).toBeInTheDocument();
    expect(screen.getByTestId('frontier-queued-skip-example.com')).toBeInTheDocument();
    expect(screen.getByTestId('frontier-queued-block-example.com')).toBeInTheDocument();
  });

  it('expands a failed domain to list its URLs with reasons', async () => {
    MockSocket.instances = [];
    const empty: {
      posts: { url: string; body: unknown }[];
      puts: { url: string; body: unknown }[];
    } = {
      posts: [],
      puts: [],
    };
    stubDomainBackend(empty);
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    fireEvent.click(await screen.findByTestId('frontier-domain-toggle-example.com'));
    await waitFor(() => {
      expect(screen.getByTestId('frontier-domain-urls-example.com')).toHaveTextContent(
        'https://example.com/a',
      );
    });
    expect(screen.getByTestId('frontier-domain-urls-example.com')).toHaveTextContent(
      'fetch timeout',
    );
    fireEvent.click(screen.getByTestId('frontier-domain-toggle-example.com'));
    await waitFor(() => {
      expect(screen.queryByTestId('frontier-domain-urls-example.com')).not.toBeInTheDocument();
    });
  });

  it('expands a queued domain to list its URLs', async () => {
    MockSocket.instances = [];
    const empty: {
      posts: { url: string; body: unknown }[];
      puts: { url: string; body: unknown }[];
    } = {
      posts: [],
      puts: [],
    };
    stubDomainBackend(empty);
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    fireEvent.click(await screen.findByTestId('frontier-queued-toggle-example.com'));
    await waitFor(() => {
      expect(screen.getByTestId('frontier-queued-urls-example.com')).toHaveTextContent(
        'https://example.com/q1',
      );
    });
    expect(screen.getByTestId('frontier-queued-urls-example.com')).toHaveTextContent(
      'https://example.com/q2',
    );
    fireEvent.click(screen.getByTestId('frontier-queued-toggle-example.com'));
    await waitFor(() => {
      expect(screen.queryByTestId('frontier-queued-urls-example.com')).not.toBeInTheDocument();
    });
  });

  it('bumps queued priority and toasts the affected count', async () => {
    MockSocket.instances = [];
    const capture = {
      posts: [] as { url: string; body: unknown }[],
      puts: [] as { url: string; body: unknown }[],
    };
    stubDomainBackend(capture);
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="secret" />);
    fireEvent.click(await screen.findByTestId('frontier-queued-prio-up-example.com'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent(
        'Priority raised — example.com: 5 row(s).',
      );
    });
    expect(capture.posts).toHaveLength(1);
    expect(capture.posts[0].url).toContain('/api/v1/frontier/domains/priority');
    expect(JSON.parse(capture.posts[0].body as string)).toMatchObject({
      domains: ['example.com'],
      delta: 1,
    });
  });
});

describe('FrontierDrawer URL inspector', () => {
  function stubInspectBackend(info: unknown): void {
    stubFetch((url: string) => {
      if (url.includes('/api/v1/frontier/url-info')) return info;
      if (url.includes('/api/v1/frontier/settings')) return { settings: SETTINGS };
      if (url.includes('/api/v1/frontier/workers')) return { workers: [], worker_count: 0 };
      return STATUS;
    });
    vi.stubGlobal('WebSocket', MockSocket);
  }

  it('shows queue, index, and fan-out for an inspected URL', async () => {
    stubInspectBackend({
      url: 'https://example.com/a',
      normalized_url: 'https://example.com/a',
      frontier: { status: 'done', priority: 9, attempts: 0, last_error: null },
      document: { title: 'Example A', chars: 11 },
      extracted_links: 2,
      link_sample: ['https://example.com/b'],
    });
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-inspect-url')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId('frontier-inspect-url'), {
      target: { value: 'https://example.com/a' },
    });
    fireEvent.click(screen.getByTestId('frontier-inspect-submit'));
    await waitFor(() => {
      expect(screen.getByTestId('frontier-inspect-result')).toBeInTheDocument();
    });
    expect(screen.getByTestId('frontier-inspect-frontier')).toHaveTextContent('done');
    expect(screen.getByTestId('frontier-inspect-document')).toHaveTextContent('Example A');
    expect(screen.getByTestId('frontier-inspect-links')).toHaveTextContent('2');
    expect(screen.getByTestId('frontier-inspect-result')).toHaveTextContent(
      'https://example.com/b',
    );
  });

  it('explains a never-seen URL instead of failing', async () => {
    stubInspectBackend({
      url: 'https://example.com/never-seen',
      normalized_url: 'https://example.com/never-seen',
      frontier: null,
      document: null,
      extracted_links: 0,
      link_sample: [],
    });
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-inspect-url')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId('frontier-inspect-url'), {
      target: { value: 'https://example.com/never-seen' },
    });
    fireEvent.click(screen.getByTestId('frontier-inspect-submit'));
    await waitFor(() => {
      expect(screen.getByTestId('frontier-inspect-result')).toBeInTheDocument();
    });
    expect(screen.getByTestId('frontier-inspect-frontier')).toHaveTextContent('never seen');
    expect(screen.getByTestId('frontier-inspect-document')).toHaveTextContent('Indexed: no');
  });
});
