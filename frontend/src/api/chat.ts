/**
 * Typed fetch wrapper for `POST {API_URL}/api/v1/agent/chat`.
 *
 * The API base URL is shared with the search client
 * (`VITE_OPENLENS_API_URL`, dev default `http://localhost:8000`).
 * No hardcoded hosts, no secrets or API keys anywhere in this module.
 */

import { API_BASE_URL, MAX_QUERY_LENGTH } from './client.ts';
import { isChatResponse, isErrorEnvelope } from './guard.ts';
import type { ChatHistoryTurn, ChatRequest, ChatResponse } from './types.ts';

/** Contract cap: at most 10 history turns per chat request. */
export const MAX_CHAT_HISTORY = 10;

/** Retrieved-hit cap default (`limit` when the caller omits it). */
export const DEFAULT_CHAT_LIMIT = 5;

/** Retrieved-hit cap ceiling (backend validates 1-10). */
export const MAX_CHAT_LIMIT = 10;

/** Options accepted by {@link buildChatBody}. */
export interface ChatOptions {
  message: string;
  history?: ChatHistoryTurn[];
  limit?: number;
}

/**
 * Validate a raw chat message against the backend contract:
 * non-blank, 1–1000 chars. Returns an error message, or `null`
 * when the message is valid.
 */
export function validateChatMessage(message: string): string | null {
  if (!message.trim()) return 'Message must not be blank.';
  if (message.length > MAX_QUERY_LENGTH) {
    return `Message must be at most ${MAX_QUERY_LENGTH} characters.`;
  }
  return null;
}

/**
 * Clamp a requested hit cap into the backend contract range 1-10.
 * Non-finite or out-of-range values degrade to {@link DEFAULT_CHAT_LIMIT}.
 */
export function clampChatLimit(limit: number | undefined): number {
  if (limit === undefined || !Number.isFinite(limit)) return DEFAULT_CHAT_LIMIT;
  const floored = Math.floor(limit);
  if (floored < 1 || floored > MAX_CHAT_LIMIT) return DEFAULT_CHAT_LIMIT;
  return floored;
}

/**
 * Shape a validated request body for `POST /api/v1/agent/chat`.
 * History is capped at the contract max (oldest turns dropped first)
 * and malformed turns are dropped instead of sent.
 */
export function buildChatBody(options: ChatOptions): ChatRequest {
  const history = (options.history ?? [])
    .filter(
      (turn): turn is ChatHistoryTurn =>
        typeof turn === 'object' &&
        turn !== null &&
        (turn.role === 'user' || turn.role === 'assistant') &&
        typeof turn.content === 'string' &&
        turn.content.length > 0 &&
        turn.content.length <= MAX_QUERY_LENGTH,
    )
    .slice(-MAX_CHAT_HISTORY);
  return {
    message: options.message,
    history,
    limit: clampChatLimit(options.limit),
  };
}

/** Machine-readable API failure raised by {@link sendChat}. */
export class ChatApiError extends Error {
  code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'ChatApiError';
    this.code = code;
  }
}

type FetchImpl = typeof fetch;

/**
 * Issue `POST {API_URL}/api/v1/agent/chat` and return the parsed
 * `ChatResponse`. Throws {@link ChatApiError} for error envelopes,
 * malformed payloads, and transport failures. A model-disabled or
 * model-error answer still resolves: the backend reports it inline
 * via `notice` (fail-closed, never 500), not as an error envelope.
 */
export async function sendChat(
  body: ChatRequest,
  fetchImpl: FetchImpl = fetch,
): Promise<ChatResponse> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE_URL}/api/v1/agent/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (error) {
    throw new ChatApiError(
      'network_error',
      error instanceof Error ? error.message : 'Network request failed.',
    );
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ChatApiError('invalid_response', 'Response was not valid JSON.');
  }

  if (isChatResponse(payload)) return payload;
  if (isErrorEnvelope(payload)) {
    throw new ChatApiError(payload.error.code, payload.error.message);
  }
  throw new ChatApiError('invalid_response', 'Unexpected response shape.');
}
