import { useEffect, useRef, useState } from 'react';
import {
  DEFAULT_CHAT_LIMIT,
  MAX_CHAT_HISTORY,
  buildChatBody,
  sendChat,
  validateChatMessage,
} from '../api/chat.ts';
import { readChatResponse } from '../api/guard.ts';
import type { ChatCitation, ChatResponse, ChatTrace } from '../api/types.ts';
import { ResultList } from './ResultList.tsx';
import { Badge } from './ui/Badge.tsx';
import { Button } from './ui/Button.tsx';
import { TextInput } from './ui/TextInput.tsx';

/** localStorage key for the persisted chat transcript. */
export const CHAT_STORAGE_KEY = 'openlens.chat.transcript.v1';

/** One transcript row: a user prompt or an assistant answer. */
export interface ChatTranscriptEntry {
  role: 'user' | 'assistant';
  content: string;
  /** Parsed assistant payload; null when the turn failed. */
  response?: ChatResponse | null;
  /** Transport-level failure for this turn; null otherwise. */
  error?: string | null;
  /** Prompt this assistant turn answered (for the search fallback). */
  prompt?: string;
}

export interface ChatViewProps {
  /**
   * Called with the user message when a fallback button is activated:
   * the parent runs it as a standard search. Omit to hide fallbacks.
   */
  onFallbackSearch?: (query: string) => void;
}

/** Loading stage copy: search first, then source reading. */
export const CHAT_LOADING_STAGES = ['Searching…', 'Reading sources…'] as const;

/** Delay before the loading copy advances to the second stage. */
export const CHAT_STAGE_DELAY_MS = 350;

/** Cap a transcript at the contract history max (newest turns kept). */
export function capTranscript(entries: ChatTranscriptEntry[]): ChatTranscriptEntry[] {
  return entries.slice(-MAX_CHAT_HISTORY);
}

/** Latest serving-model label reported in the transcript, if any. */
export function transcriptModel(entries: ChatTranscriptEntry[]): string | null {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const model = entries[index]?.response?.trace?.model;
    if (typeof model === 'string' && model !== '') return model;
  }
  return null;
}

function isTranscriptEntry(value: unknown): value is ChatTranscriptEntry {
  if (typeof value !== 'object' || value === null) return false;
  const entry = value as Record<string, unknown>;
  if (entry['role'] !== 'user' && entry['role'] !== 'assistant') return false;
  if (typeof entry['content'] !== 'string') return false;
  return true;
}

/** Load the persisted transcript; malformed entries drop, never throw. */
export function loadTranscript(): ChatTranscriptEntry[] {
  try {
    const raw = localStorage.getItem(CHAT_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const entries = (parsed as unknown[]).filter(isTranscriptEntry).map((entry) => {
      if (entry.role !== 'assistant' || entry.response === undefined) return entry;
      const narrowed = readChatResponse(entry.response);
      return { ...entry, response: narrowed };
    });
    return capTranscript(entries);
  } catch {
    return [];
  }
}

/** Split answer text on `[n]` markers, keeping the markers. */
export function splitAnswer(answer: string): string[] {
  return answer.split(/(\[\d+\])/);
}

/** Citation matching an in-text `[n]` marker, or undefined. */
function citationForRef(citations: ChatCitation[], ref: number): ChatCitation | undefined {
  return citations.find((citation) => citation.ref === ref);
}

/**
 * Answer paragraph with in-text `[n]` markers linked to their citation
 * URLs (new tab, `noopener noreferrer`). Markers without a matching
 * citation, or with a null URL, render as plain text.
 */
export function AnswerText({
  answer,
  citations,
  messageIndex,
}: {
  answer: string;
  citations: ChatCitation[];
  messageIndex: number;
}) {
  return (
    <p data-testid="chat-answer" className="m-0 whitespace-pre-wrap">
      {splitAnswer(answer).map((part, index) => {
        const match = /^\[(\d+)\]$/.exec(part);
        if (!match?.[1]) return <span key={`${messageIndex}-${index}`}>{part}</span>;
        const ref = Number(match[1]);
        const citation = citationForRef(citations, ref);
        if (!citation || !citation.url) return <span key={`${messageIndex}-${index}`}>{part}</span>;
        return (
          <a
            key={`${messageIndex}-${index}`}
            href={citation.url}
            target="_blank"
            rel="noopener noreferrer"
            data-testid={`chat-answer-cite-${ref}`}
            aria-label={`Source ${ref}: ${citation.title}`}
          >
            {part}
          </a>
        );
      })}
    </p>
  );
}

/**
 * Citation chip row: `[n] title` links for every citation with a URL,
 * plain text otherwise. Null-safe: renders nothing when empty.
 */
export function CitationChips({ citations }: { citations: ChatCitation[] }) {
  if (citations.length === 0) return null;
  return (
    <ul data-testid="chat-citations" className="my-ol-s flex list-none flex-wrap gap-ol-xs p-0">
      {citations.map((citation) => (
        <li key={citation.ref}>
          {citation.url ? (
            <a
              href={citation.url}
              target="_blank"
              rel="noopener noreferrer"
              data-testid={`chat-citation-${citation.ref}`}
              className="ol-badge text-ol-s font-semibold rounded-ol-s px-ol-s leading-[1.6] bg-ol-well border border-transparent text-ol-ink-soft inline-block no-underline"
            >
              [{citation.ref}] {citation.title}
            </a>
          ) : (
            <span
              data-testid={`chat-citation-${citation.ref}`}
              className="ol-badge text-ol-s font-semibold rounded-ol-s px-ol-s leading-[1.6] bg-ol-well border border-transparent text-ol-ink-soft inline-block"
            >
              [{citation.ref}] {citation.title}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

/**
 * Trace section reusing the agent-trace UI pattern (badged kinds and
 * fallbacks, rewritten query, model calls). Every field optional:
 * renders nothing when the trace carries nothing usable.
 */
export function ChatTraceView({ trace }: { trace: ChatTrace | null | undefined }) {
  if (!trace || Object.keys(trace).length === 0) return null;
  const filterKeys =
    trace.filters && Object.keys(trace.filters).length > 0 ? Object.keys(trace.filters).sort() : [];
  return (
    <div data-testid="chat-trace" className="mt-ol-s">
      <h4 className="mt-ol-m mb-ol-s text-ol-m">Agent trace</h4>
      {trace.rewritten_query ? (
        <p data-testid="chat-trace-rewritten" className="my-ol-s">
          Rewritten query: {trace.rewritten_query}
        </p>
      ) : null}
      {trace.model_calls !== undefined ? (
        <p data-testid="chat-trace-model-calls" className="my-ol-s">
          Model calls: {trace.model_calls}
        </p>
      ) : null}
      {filterKeys.length > 0 && trace.filters ? (
        <dl
          data-testid="chat-trace-filters"
          className="grid grid-cols-[auto_1fr] gap-x-ol-m gap-y-ol-xs m-0 my-ol-s"
        >
          {filterKeys.map((key) => (
            <div key={key} className="contents">
              <dt>{key}</dt>
              <dd>{JSON.stringify(trace.filters?.[key])}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {trace.steps && trace.steps.length > 0 ? (
        <ol data-testid="chat-trace-steps" className="flex flex-col gap-ol-xs my-ol-s pl-ol-m">
          {trace.steps.map((step, index) => (
            <li key={`${step.kind ?? 'step'}-${index}`}>
              {step.kind ? <Badge>{step.kind}</Badge> : null} {step.detail ?? step.kind ?? 'step'}
            </li>
          ))}
        </ol>
      ) : null}
      {trace.fallbacks && trace.fallbacks.length > 0 ? (
        <ul data-testid="chat-trace-fallbacks" className="flex flex-col gap-ol-xs my-ol-s pl-ol-m">
          {trace.fallbacks.map((fallback) => (
            <li key={fallback}>
              <Badge>Fallback</Badge> {fallback}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/**
 * AI Mode chat: transcript persisted to localStorage, Enter-to-send,
 * staged loading copy, citation chips, collapsible raw results, and
 * fail-closed notice/error rendering with a one-click standard-search
 * fallback. Styling reuses tailwind utilities only — no custom CSS.
 */
export default function ChatView({ onFallbackSearch }: ChatViewProps) {
  const [transcript, setTranscript] = useState<ChatTranscriptEntry[]>(loadTranscript);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [stage, setStage] = useState(0);
  const [validationError, setValidationError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const servingModel = transcriptModel(transcript);

  // Persist every transcript change, capped at the contract max.
  useEffect(() => {
    try {
      localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(capTranscript(transcript)));
    } catch {
      // Private-mode storage failures must never break the chat.
    }
  }, [transcript]);

  // Auto-scroll to the latest message on every transcript change.
  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [transcript, loading]);

  async function send(message: string) {
    const invalid = validateChatMessage(message);
    if (invalid !== null) {
      setValidationError(invalid);
      return;
    }
    setValidationError(null);
    const history = transcript
      .filter((entry) => entry.content.length > 0)
      .map((entry) => ({ role: entry.role, content: entry.content }));
    const body = buildChatBody({ message, history, limit: DEFAULT_CHAT_LIMIT });
    const next: ChatTranscriptEntry[] = [...transcript, { role: 'user', content: message }];
    setTranscript(capTranscript(next));
    setInput('');
    setLoading(true);
    setStage(0);
    const timer = window.setTimeout(() => setStage(1), CHAT_STAGE_DELAY_MS);
    try {
      const response = await sendChat(body);
      setTranscript((current) =>
        capTranscript([
          ...current,
          { role: 'assistant', content: response.answer, response, prompt: message },
        ]),
      );
    } catch (error) {
      const detail = error instanceof Error ? error.message : 'Unexpected chat failure.';
      setTranscript((current) =>
        capTranscript([
          ...current,
          { role: 'assistant', content: '', response: null, error: detail, prompt: message },
        ]),
      );
    } finally {
      window.clearTimeout(timer);
      setLoading(false);
    }
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (loading) return;
    void send(input);
  }

  return (
    <section aria-label="AI chat" data-testid="chat-view" className="flex min-w-0 flex-col gap-4">
      {transcript.length === 0 && !loading ? (
        <p data-testid="chat-empty" className="text-ol-s text-ol-ink-soft m-0">
          Ask a question in plain language. Answers are grounded in retrieved sources — every claim
          links back with a [n] citation chip you can open in a new tab.
        </p>
      ) : null}
      <div
        ref={scrollRef}
        data-testid="chat-transcript"
        aria-live="polite"
        className="flex max-h-[60vh] min-w-0 flex-col gap-4 overflow-y-auto"
      >
        {transcript.map((entry, index) =>
          entry.role === 'user' ? (
            <div key={`user-${index}`} data-testid="chat-message-user" className="self-end">
              <p className="bg-ol-well rounded-ol-m px-ol-m py-ol-s m-0">{entry.content}</p>
            </div>
          ) : (
            <div
              key={`assistant-${index}`}
              data-testid="chat-message-assistant"
              className="border-ol-line min-w-0 rounded-ol-m border px-ol-m py-ol-s"
            >
              {entry.error ? (
                <>
                  <p data-testid="chat-error" role="alert" className="m-0">
                    Chat failed: {entry.error}
                  </p>
                  {entry.prompt && onFallbackSearch ? (
                    <p className="my-ol-s">
                      <Button
                        variant="secondary"
                        data-testid="chat-fallback"
                        onClick={() => onFallbackSearch(entry.prompt as string)}
                      >
                        Run as standard search
                      </Button>
                    </p>
                  ) : null}
                </>
              ) : entry.response?.notice ? (
                <>
                  <p data-testid="chat-notice" role="status" className="m-0">
                    {entry.response.notice}
                  </p>
                  {entry.response.results.length > 0 ? (
                    <details data-testid="chat-raw-results" className="mt-ol-s">
                      <summary className="cursor-pointer font-semibold text-ol-s">
                        Sources ({entry.response.results.length})
                      </summary>
                      <ResultList
                        results={entry.response.results}
                        total={entry.response.results.length}
                      />
                    </details>
                  ) : null}
                  {entry.prompt && onFallbackSearch ? (
                    <p className="my-ol-s">
                      <Button
                        variant="secondary"
                        data-testid="chat-fallback"
                        onClick={() => onFallbackSearch(entry.prompt as string)}
                      >
                        Run as standard search
                      </Button>
                    </p>
                  ) : null}
                </>
              ) : entry.response ? (
                <>
                  {entry.response.answer ? (
                    <AnswerText
                      answer={entry.response.answer}
                      citations={entry.response.citations}
                      messageIndex={index}
                    />
                  ) : null}
                  <CitationChips citations={entry.response.citations} />
                  {entry.response.results.length > 0 ? (
                    <details data-testid="chat-raw-results" className="mt-ol-s">
                      <summary className="cursor-pointer font-semibold text-ol-s">
                        Sources ({entry.response.results.length})
                      </summary>
                      <ResultList
                        results={entry.response.results}
                        total={entry.response.results.length}
                      />
                    </details>
                  ) : null}
                  <ChatTraceView trace={entry.response.trace} />
                </>
              ) : null}
            </div>
          ),
        )}
        {loading ? (
          <p
            data-testid="chat-loading"
            aria-live="polite"
            className="text-ol-s text-ol-ink-soft m-0"
          >
            {CHAT_LOADING_STAGES[stage] ?? CHAT_LOADING_STAGES[0]}
          </p>
        ) : null}
      </div>
      <form
        aria-label="AI chat"
        className="ol-search-form flex items-end gap-2"
        onSubmit={handleSubmit}
        noValidate
      >
        <TextInput
          label="Chat message"
          id="openlens-chat-input"
          name="message"
          type="text"
          value={input}
          maxLength={1000}
          autoComplete="off"
          placeholder="Ask anything…"
          onChange={(event: React.ChangeEvent<HTMLInputElement>) => setInput(event.target.value)}
          disabled={loading}
        />
        <Button variant="primary" type="submit" disabled={loading}>
          Send
        </Button>
      </form>
      {validationError ? (
        <p id="openlens-chat-error" role="alert" className="ol-field-error m-0">
          {validationError}
        </p>
      ) : null}
      <p data-testid="chat-powered-by" className="text-ol-s text-ol-ink-soft m-0">
        {servingModel ? `Powered by ${servingModel}` : 'Powered by AI'}
      </p>
    </section>
  );
}
