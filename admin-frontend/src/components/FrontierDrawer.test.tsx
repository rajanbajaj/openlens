import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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

  it('pauses and clears the queue behind a confirm', async () => {
    MockSocket.instances = [];
    stubBackend({ ...STATUS, cleared_queued: 2, kafka_purged: true });
    vi.stubGlobal('WebSocket', MockSocket);
    vi.stubGlobal(
      'confirm',
      vi.fn(() => true),
    );
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByText('Pause & clear')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('Pause & clear'));
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
    vi.stubGlobal(
      'confirm',
      vi.fn(() => false),
    );
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByText('Pause & clear')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('Pause & clear'));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(clearCalls).toBe(0);
  });

  it('confirms a successful seed resume with a seed count', async () => {
    MockSocket.instances = [];
    stubBackend(STATUS);
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
});

describe('FrontierDrawer settings form', () => {
  it('shows the loaded User-Agent and saves edits back', async () => {
    MockSocket.instances = [];
    let putBody: unknown = null;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string, init?: { body?: string }) => {
        if (url.includes('/api/v1/frontier/settings') && init?.body) {
          putBody = JSON.parse(init.body);
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: new Headers(),
          text: () => Promise.resolve(JSON.stringify({ settings: SETTINGS })),
          json: () =>
            Promise.resolve(
              url.includes('/api/v1/frontier/settings')
                ? { settings: SETTINGS }
                : url.includes('/api/v1/frontier/workers')
                  ? { workers: [], worker_count: 0 }
                  : STATUS,
            ),
        });
      }),
    );
    vi.stubGlobal('WebSocket', MockSocket);
    render(<FrontierDrawer adminToken="" />);
    const input = (await screen.findByTestId('frontier-setting-user-agent')) as HTMLInputElement;
    expect(input.value).toBe('TestBot/1.0 (+https://example.com)');
    fireEvent.change(input, { target: { value: 'OtherBot/2.0 (+https://example.com)' } });
    fireEvent.click(screen.getByText('Save settings'));
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
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: new Headers(),
          text: () => Promise.resolve(JSON.stringify({ settings: SETTINGS })),
          json: () =>
            Promise.resolve(
              url.includes('/api/v1/frontier/settings') ? { settings: SETTINGS } : { ...STATUS },
            ),
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
    fireEvent.click(screen.getByText('Add'));
    expect(screen.getByTestId('frontier-skip-list')).toHaveTextContent('ads.example.com');

    fireEvent.change(input, { target: { value: 'ads.example.com' } });
    fireEvent.click(screen.getByText('Add'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent('already skipped');
    });

    fireEvent.click(screen.getByTestId('frontier-skip-remove-ads.example.com'));
    expect(screen.queryByTestId('frontier-skip-list')).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: 'tracker.example' } });
    fireEvent.click(screen.getByText('Add'));
    fireEvent.click(screen.getByText('Save settings'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent('Settings saved.');
    });
    expect(capture.body).toMatchObject({ skip_domains: ['tracker.example'] });
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
});
