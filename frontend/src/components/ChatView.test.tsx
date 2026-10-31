import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ChatView, { CHAT_STORAGE_KEY, capTranscript } from './ChatView.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

beforeEach(() => {
  localStorage.clear();
});

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
  answer: 'It costs $9 [1] today.',
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

function stubChat(payload: unknown) {
  const stub = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(payload) });
  vi.stubGlobal('fetch', stub);
  return stub;
}

function chatForm(): HTMLFormElement {
  const form = screen.getByTestId('chat-view').querySelector('form');
  if (!form) throw new Error('chat form missing');
  return form;
}

async function sendMessage(message: string) {
  fireEvent.change(screen.getByLabelText(/chat message/i), { target: { value: message } });
  // Native Enter-to-send submits the form; fire the submit directly.
  fireEvent.submit(chatForm());
}

describe('ChatView send/render flow', () => {
  it('posts the contract body and renders the answer with trace', async () => {
    const fetchStub = stubChat(CHAT_OK);
    render(<ChatView />);
    expect(screen.getByTestId('chat-empty')).toBeInTheDocument();

    await sendMessage('what does it cost?');

    await waitFor(() => {
      expect(screen.getByTestId('chat-answer')).toBeInTheDocument();
    });
    const [url, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    expect(url).toContain('/api/v1/agent/chat');
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body).toEqual({ message: 'what does it cost?', history: [], limit: 5 });
    expect(screen.getByTestId('chat-message-user')).toHaveTextContent('what does it cost?');
    expect(screen.getByTestId('chat-answer')).toHaveTextContent('It costs $9');
    expect(screen.getByTestId('chat-trace-rewritten')).toHaveTextContent('price today?');
    expect(screen.getByTestId('chat-trace-model-calls')).toHaveTextContent('Model calls: 1');
    expect(screen.getByTestId('chat-trace-steps')).toHaveTextContent('queried mock');
    // Raw results reuse the result cards behind a disclosure.
    expect(screen.getByTestId('chat-raw-results')).toHaveTextContent('Sources (1)');
    expect(screen.getByTestId('chat-raw-results')).toHaveTextContent('Price page');
    expect(screen.queryByTestId('chat-empty')).not.toBeInTheDocument();
  });

  it('sends prior turns as capped history on follow-ups', async () => {
    const fetchStub = stubChat(CHAT_OK);
    render(<ChatView />);
    await sendMessage('first?');
    await waitFor(() => {
      expect(screen.getByTestId('chat-answer')).toBeInTheDocument();
    });
    await sendMessage('second?');
    await waitFor(() => {
      expect(fetchStub).toHaveBeenCalledTimes(2);
    });
    const [, init] = fetchStub.mock.calls[1] as [string, RequestInit];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body['history']).toEqual([
      { role: 'user', content: 'first?' },
      { role: 'assistant', content: CHAT_OK.answer },
    ]);
  });

  it('shows staged loading copy while the request is in flight', async () => {
    let resolveFetch!: (value: unknown) => void;
    const pending = new Promise((resolve) => {
      resolveFetch = resolve;
    });
    const stub = vi
      .fn()
      .mockReturnValue(pending.then(() => ({ ok: true, json: () => Promise.resolve(CHAT_OK) })));
    vi.stubGlobal('fetch', stub);
    render(<ChatView />);
    await sendMessage('slow question?');

    await waitFor(() => {
      expect(screen.getByTestId('chat-loading')).toHaveTextContent('Searching…');
    });
    expect(await screen.findByText('Reading sources…', {}, { timeout: 3000 })).toBeInTheDocument();
    resolveFetch(undefined);
    await waitFor(() => {
      expect(screen.getByTestId('chat-answer')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('chat-loading')).not.toBeInTheDocument();
  });

  it('rejects blank messages without fetching', async () => {
    const fetchStub = stubChat(CHAT_OK);
    render(<ChatView />);
    fireEvent.change(screen.getByLabelText(/chat message/i), { target: { value: '   ' } });
    fireEvent.submit(chatForm());
    expect(await screen.findByRole('alert')).toHaveTextContent('Message must not be blank.');
    expect(fetchStub).not.toHaveBeenCalled();
  });
});

describe('ChatView citations', () => {
  it('chips link to result URLs in a new tab with noopener', async () => {
    stubChat({
      ...CHAT_OK,
      citations: [
        { ref: 1, title: 'Price page', url: 'https://example.com/x' },
        { ref: 2, title: 'No link yet', url: null },
      ],
    });
    render(<ChatView />);
    await sendMessage('links?');
    const chip = await screen.findByTestId('chat-citation-1');
    expect(chip.tagName).toBe('A');
    expect(chip).toHaveAttribute('href', 'https://example.com/x');
    expect(chip).toHaveAttribute('target', '_blank');
    expect(chip.getAttribute('rel')).toContain('noopener');
    expect(chip).toHaveTextContent('[1] Price page');
    // Null-URL citations render as plain text, never a dead link.
    const plain = screen.getByTestId('chat-citation-2');
    expect(plain.tagName).not.toBe('A');
    expect(plain).toHaveTextContent('[2] No link yet');
    // The in-text [1] marker links to the same source URL.
    const inline = screen.getByTestId('chat-answer-cite-1');
    expect(inline).toHaveAttribute('href', 'https://example.com/x');
    expect(inline).toHaveAttribute('target', '_blank');
  });
});

describe('ChatView persistence', () => {
  it('round-trips the transcript through localStorage', async () => {
    stubChat(CHAT_OK);
    const { unmount } = render(<ChatView />);
    await sendMessage('remember me?');
    await waitFor(() => {
      expect(screen.getByTestId('chat-answer')).toBeInTheDocument();
    });
    const stored = JSON.parse(localStorage.getItem(CHAT_STORAGE_KEY) as string) as unknown[];
    expect(stored.length).toBeGreaterThanOrEqual(2);
    unmount();
    cleanup();

    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new Error('must not fetch on reload')),
    );
    render(<ChatView />);
    // Reloaded from storage: no fetch, transcript intact.
    expect(screen.getByTestId('chat-message-user')).toHaveTextContent('remember me?');
    expect(screen.getByTestId('chat-answer')).toHaveTextContent('It costs $9');
  });

  it('caps stored turns at the contract max', () => {
    const entries = Array.from({ length: 14 }, (_, i) => ({
      role: (i % 2 === 0 ? 'user' : 'assistant') as 'user' | 'assistant',
      content: `turn ${i}`,
    }));
    const capped = capTranscript(entries);
    expect(capped).toHaveLength(10);
    expect(capped[0]?.content).toBe('turn 4');
  });
});

describe('ChatView failure rendering', () => {
  it('renders the notice with a one-click standard-search fallback', async () => {
    const onFallbackSearch = vi.fn();
    stubChat({ answer: '', citations: [], results: [HIT], trace: {}, notice: 'Model off.' });
    render(<ChatView onFallbackSearch={onFallbackSearch} />);
    await sendMessage('fallback please?');

    const notice = await screen.findByTestId('chat-notice');
    expect(notice).toHaveTextContent('Model off.');
    // Fail-closed: no answer, but plain-search hits stay visible.
    expect(screen.queryByTestId('chat-answer')).not.toBeInTheDocument();
    expect(screen.getByTestId('chat-raw-results')).toHaveTextContent('Price page');
    fireEvent.click(screen.getByTestId('chat-fallback'));
    expect(onFallbackSearch).toHaveBeenCalledWith('fallback please?');
  });

  it('renders transport errors with the same fallback', async () => {
    const onFallbackSearch = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('down')));
    render(<ChatView onFallbackSearch={onFallbackSearch} />);
    await sendMessage('broken?');

    expect(await screen.findByTestId('chat-error')).toHaveTextContent('down');
    fireEvent.click(screen.getByTestId('chat-fallback'));
    expect(onFallbackSearch).toHaveBeenCalledWith('broken?');
  });
});
