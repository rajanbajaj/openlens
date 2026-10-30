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
};

/** Minimal socket stand-in: the drawer only needs onopen/onmessage/close. */
class MockSocket {
  static instances: MockSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  url: string;
  closed = false;
  constructor(url: string) {
    this.url = url;
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
    render(<FrontierDrawer adminToken="" />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-queue')).toHaveTextContent('example.com/a');
    });
    expect(MockSocket.instances).toHaveLength(1);
    expect(MockSocket.instances[0].url).toBe('ws://localhost:8000/api/v1/frontier/live');

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
