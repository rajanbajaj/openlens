import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AiSummaryPanel from './AiSummaryPanel.tsx';
import { CHAT_STORAGE_KEY, loadTranscript } from './ChatView.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

beforeEach(() => {
  localStorage.clear();
});

const CHAT_OK = {
  answer: 'Top picks compared [1].',
  citations: [{ ref: 1, title: 'Price page', url: 'https://x.example.com/x' }],
  results: [],
  trace: { rewritten_query: 'headphones', model: 'muse-spark', model_calls: 1 },
  notice: null,
};

function stubChat(payload: unknown) {
  const stub = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(payload) });
  vi.stubGlobal('fetch', stub);
  return stub;
}

describe('AiSummaryPanel', () => {
  it('summarizes on demand and offers continuing in chat', async () => {
    const onContinue = vi.fn();
    const fetchStub = stubChat(CHAT_OK);
    render(<AiSummaryPanel query="headphones" onContinueInChat={onContinue} />);

    expect(screen.getByTestId('ai-summary-start')).toBeInTheDocument();
    expect(fetchStub).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('ai-summary-start'));
    expect(screen.getByTestId('ai-summary-loading')).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByTestId('chat-answer')).toHaveTextContent('Top picks compared');
    });
    const [, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string).message).toContain('headphones');
    expect(screen.getByTestId('ai-summary-powered-by')).toHaveTextContent('muse-spark');
    expect(screen.getByTestId('chat-citation-1')).toHaveTextContent('Price page');

    fireEvent.click(screen.getByTestId('ai-summary-continue'));
    expect(onContinue).toHaveBeenCalledTimes(1);
    const seeded = loadTranscript();
    expect(seeded).toHaveLength(2);
    expect(seeded[0]).toMatchObject({ role: 'user' });
    expect(seeded[0]?.content).toContain('headphones');
    expect(seeded[1]).toMatchObject({ role: 'assistant' });
    expect(seeded[1]?.content).toBe('Top picks compared [1].');
  });

  it('shows the backend notice without a continue button when no answer', async () => {
    stubChat({ ...CHAT_OK, answer: '', notice: 'no model backend enabled' });
    render(<AiSummaryPanel query="headphones" onContinueInChat={() => {}} />);

    fireEvent.click(screen.getByTestId('ai-summary-start'));
    await waitFor(() => {
      expect(screen.getByTestId('ai-summary-notice')).toHaveTextContent('no model backend enabled');
    });
    expect(screen.queryByTestId('ai-summary-continue')).not.toBeInTheDocument();
    expect(loadTranscript()).toHaveLength(0);
  });

  it('shows no powered-by line when no model served the summary', async () => {
    stubChat({ ...CHAT_OK, trace: { ...CHAT_OK.trace, model: '' } });
    render(<AiSummaryPanel query="headphones" onContinueInChat={() => {}} />);

    fireEvent.click(screen.getByTestId('ai-summary-start'));
    await waitFor(() => {
      expect(screen.getByTestId('chat-answer')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('ai-summary-powered-by')).not.toBeInTheDocument();
  });

  it('reports transport failures without throwing', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('down')));
    render(<AiSummaryPanel query="headphones" onContinueInChat={() => {}} />);

    fireEvent.click(screen.getByTestId('ai-summary-start'));
    await waitFor(() => {
      expect(screen.getByTestId('ai-summary-error')).toHaveTextContent('down');
    });
  });

  it('merges seeded turns with an existing transcript', async () => {
    localStorage.setItem(
      CHAT_STORAGE_KEY,
      JSON.stringify([{ role: 'user', content: 'earlier question' }]),
    );
    stubChat(CHAT_OK);
    render(<AiSummaryPanel query="headphones" onContinueInChat={() => {}} />);

    fireEvent.click(screen.getByTestId('ai-summary-start'));
    await waitFor(() => {
      expect(screen.getByTestId('ai-summary-continue')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('ai-summary-continue'));
    const seeded = loadTranscript();
    expect(seeded.map((entry) => entry.content)).toEqual([
      'earlier question',
      expect.stringContaining('headphones'),
      'Top picks compared [1].',
    ]);
  });
});
