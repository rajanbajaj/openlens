import { useState } from 'react';
import { buildChatBody, sendChat, validateChatMessage } from '../api/chat.ts';
import type { ChatResponse } from '../api/types.ts';
import { Button } from './ui/Button.tsx';
import { AnswerText, CitationChips, seedChatTranscript } from './ChatView.tsx';

export interface AiSummaryPanelProps {
  /** The submitted search query the summary covers. */
  query: string;
  /** Called after the summary exchange is seeded into the chat transcript. */
  onContinueInChat: () => void;
}

/** Prompt sent to the agent; the agent re-retrieves and grounds the answer. */
export function summaryPrompt(query: string): string {
  return `Summarize the top results for "${query}"`;
}

type Status = 'idle' | 'loading' | 'done' | 'error';

/**
 * On-demand AI summary of the current search page, rendered above the
 * results. On-demand (not automatic) so offline/model-disabled setups
 * never pay for or wait on a model call they did not ask for; the
 * agent grounds the summary in retrieved sources with citations.
 * "Continue in AI chat" seeds the exchange into the chat transcript
 * and hands off to the chat view for follow-ups.
 */
export default function AiSummaryPanel({ query, onContinueInChat }: AiSummaryPanelProps) {
  const [status, setStatus] = useState<Status>('idle');
  const [response, setResponse] = useState<ChatResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function summarize() {
    if (status === 'loading') return;
    const message = summaryPrompt(query);
    const invalid = validateChatMessage(message);
    if (invalid !== null) {
      setError(invalid);
      setStatus('error');
      return;
    }
    setStatus('loading');
    setError(null);
    try {
      const answer = await sendChat(buildChatBody({ message }));
      setResponse(answer);
      setStatus('done');
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Summary request failed.');
      setStatus('error');
    }
  }

  function continueInChat() {
    if (!response || !response.answer) return;
    const message = summaryPrompt(query);
    seedChatTranscript([
      { role: 'user', content: message },
      { role: 'assistant', content: response.answer, response, prompt: message },
    ]);
    onContinueInChat();
  }

  const model =
    response?.trace && typeof response.trace.model === 'string' && response.trace.model !== ''
      ? response.trace.model
      : null;

  return (
    <section aria-label="AI result summary" data-testid="ai-summary" className="min-w-0">
      {status === 'idle' ? (
        <p className="m-0">
          <Button variant="secondary" data-testid="ai-summary-start" onClick={summarize}>
            Summarize results with AI
          </Button>
        </p>
      ) : null}
      {status === 'loading' ? (
        <p data-testid="ai-summary-loading" aria-live="polite" className="m-0">
          Summarizing top results…
        </p>
      ) : null}
      {status === 'error' && error ? (
        <p data-testid="ai-summary-error" role="alert" className="ol-field-error m-0">
          {error}
        </p>
      ) : null}
      {status === 'done' && response ? (
        <div className="border-ol-line min-w-0 rounded-ol-m border px-ol-m py-ol-s">
          {response.notice ? (
            <p data-testid="ai-summary-notice" role="status" className="m-0">
              {response.notice}
            </p>
          ) : null}
          {response.answer ? (
            <AnswerText answer={response.answer} citations={response.citations} messageIndex={0} />
          ) : null}
          <CitationChips citations={response.citations} />
          <p data-testid="ai-summary-powered-by" className="text-ol-s text-ol-ink-soft m-0">
            {model ? `Powered by ${model}` : 'Powered by AI'}
          </p>
          {response.answer ? (
            <p className="my-ol-s">
              <Button
                variant="secondary"
                data-testid="ai-summary-continue"
                onClick={continueInChat}
              >
                Continue in AI chat
              </Button>
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
