import { useEffect, useRef, useState } from 'react';
import {
  MAX_QUERY_LENGTH,
  SUGGEST_DEBOUNCE_MS,
  fetchSuggestions,
  validateQuery,
} from '../api/client.ts';
import { Button } from './ui/Button.tsx';
import { TextInput } from './ui/TextInput.tsx';
import SuggestDropdown, { moveSuggestHighlight } from './SuggestDropdown.tsx';

export interface SearchFormProps {
  /** Current raw query text (controlled by the parent). */
  value: string;
  /** Called on every keystroke in the query input. */
  onChange: (query: string) => void;
  /** Called with the trimmed query when validation passes. */
  onSubmit: (query: string) => void;
  /** Server-side or async error to display below the input, if any. */
  externalError?: string | null;
  /** Disables the input and submit button while a search is in flight. */
  disabled?: boolean;
  /** Extra class merged onto the form for hero vs compact-header contexts. */
  className?: string;
  /**
   * Called with the picked completion when the user selects a
   * suggestion (keyboard Enter or click). When omitted, selecting a
   * suggestion falls back to `onSubmit`, so direct search still works.
   */
  onSuggestSelect?: (suggestion: string) => void;
}

/** Queries shorter than this never fetch: silence over tiny indexes. */
const SUGGEST_MIN_LENGTH = 2;

const SUGGEST_LIST_ID = 'openlens-suggest-listbox';

/**
 * Query input with client-side validation (non-blank, ≤1000 chars) plus
 * as-you-type suggestions from `GET /api/v1/suggest`.
 *
 * The form owns the debounce timer and the abort of stale fetches: every
 * value change restarts the `SUGGEST_DEBOUNCE_MS` timer, and every fetch
 * start aborts the previous in-flight request. A sequence guard drops
 * out-of-order responses, so a slow first fetch never overwrites a fresh
 * one. Blank/short queries, backend-absent suggest (404/5xx), malformed
 * payloads, and aborts all resolve to an empty list — the dropdown stays
 * silent and the form still submits directly.
 *
 * Keyboard map on the input: ArrowDown/ArrowUp move the highlight
 * (wrapping), Enter picks the highlighted suggestion, Escape closes the
 * dropdown. Mounted once per page — either in the empty-state hero or in
 * the post-search sticky header — so its input/error ids stay unique.
 */
export default function SearchForm({
  value,
  onChange,
  onSubmit,
  externalError = null,
  disabled = false,
  className,
  onSuggestSelect,
}: SearchFormProps) {
  const validationError = validateQuery(value);
  const error = (value.length === 0 ? null : validationError) ?? externalError;
  const invalid = error !== null;

  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [suggestLoading, setSuggestLoading] = useState(false);
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const seqRef = useRef(0);

  // Keep the latest callbacks for the debounced fetch without
  // re-arming the timer when only the callbacks change identity.
  const callbacksRef = useRef({ onChange, onSubmit, onSuggestSelect });
  callbacksRef.current = { onChange, onSubmit, onSuggestSelect };

  function cancelSuggestFetch() {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    abortRef.current?.abort();
    abortRef.current = null;
  }

  // Abort pending work on unmount so no late response touches state.
  useEffect(
    () => () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      abortRef.current?.abort();
    },
    [],
  );

  // Debounced, cancellable suggestion fetch owned by the form.
  useEffect(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    if (value.trim().length < SUGGEST_MIN_LENGTH) {
      abortRef.current?.abort();
      abortRef.current = null;
      setSuggestions([]);
      setSuggestLoading(false);
      setSuggestOpen(false);
      setActiveIndex(-1);
      return;
    }
    setSuggestLoading(true);
    setSuggestOpen(true);
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      const seq = seqRef.current + 1;
      seqRef.current = seq;
      void fetchSuggestions(value, { signal: controller.signal }).then((completions) => {
        // Stale guard: a slow earlier fetch loses to the latest one.
        if (seqRef.current !== seq) return;
        abortRef.current = null;
        setSuggestions(completions);
        setActiveIndex(-1);
        setSuggestLoading(false);
        setSuggestOpen(true);
      });
    }, SUGGEST_DEBOUNCE_MS);
    return () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [value]);

  function selectSuggestion(suggestion: string) {
    cancelSuggestFetch();
    setSuggestions([]);
    setSuggestLoading(false);
    setSuggestOpen(false);
    setActiveIndex(-1);
    const callbacks = callbacksRef.current;
    callbacks.onChange(suggestion);
    if (callbacks.onSuggestSelect) {
      callbacks.onSuggestSelect(suggestion);
    } else {
      callbacks.onSubmit(suggestion);
    }
  }

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const message = validateQuery(value);
    if (message !== null) return;
    cancelSuggestFetch();
    setSuggestions([]);
    setSuggestLoading(false);
    setSuggestOpen(false);
    setActiveIndex(-1);
    onSubmit(value);
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    const arrowKey = event.key === 'ArrowDown' || event.key === 'ArrowUp' ? event.key : null;
    if (arrowKey !== null) {
      if (!suggestOpen || (suggestions.length === 0 && !suggestLoading)) return;
      event.preventDefault();
      setSuggestOpen(true);
      setActiveIndex((current) => moveSuggestHighlight(current, arrowKey, suggestions.length));
    } else if (event.key === 'Enter') {
      if (suggestOpen && activeIndex >= 0 && activeIndex < suggestions.length) {
        event.preventDefault();
        const picked = suggestions[activeIndex];
        if (picked !== undefined) selectSuggestion(picked);
      }
    } else if (event.key === 'Escape') {
      if (suggestOpen) {
        event.preventDefault();
        setSuggestOpen(false);
        setActiveIndex(-1);
      }
    }
  };

  const showDropdown = suggestOpen && (suggestLoading || suggestions.length > 0);
  const activeOptionId =
    activeIndex >= 0 && activeIndex < suggestions.length
      ? `${SUGGEST_LIST_ID}-option-${activeIndex}`
      : undefined;

  return (
    <form
      role="search"
      aria-label="OpenLens search"
      className={className ? `ol-search-form ${className}` : 'ol-search-form'}
      onSubmit={handleSubmit}
      noValidate
    >
      <TextInput
        label="Search query"
        id="openlens-query"
        name="query"
        type="search"
        value={value}
        maxLength={MAX_QUERY_LENGTH}
        autoComplete="off"
        role="combobox"
        aria-expanded={showDropdown}
        aria-controls={showDropdown ? SUGGEST_LIST_ID : undefined}
        aria-activedescendant={showDropdown ? activeOptionId : undefined}
        aria-invalid={invalid}
        aria-describedby={invalid ? 'openlens-query-error' : undefined}
        onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
          onChange(event.target.value);
        }}
        onKeyDown={handleKeyDown}
        onBlur={() => {
          setSuggestOpen(false);
          setActiveIndex(-1);
        }}
        onFocus={() => {
          if (value.trim().length >= SUGGEST_MIN_LENGTH) setSuggestOpen(true);
        }}
        disabled={disabled}
      />
      {showDropdown ? (
        <SuggestDropdown
          suggestions={suggestions}
          activeIndex={activeIndex}
          loading={suggestLoading}
          listId={SUGGEST_LIST_ID}
          onSelect={selectSuggestion}
          onActiveChange={setActiveIndex}
        />
      ) : null}
      <Button variant="primary" type="submit" disabled={disabled}>
        Search
      </Button>
      {invalid && (
        <p id="openlens-query-error" role="alert" className="ol-field-error">
          {error}
        </p>
      )}
    </form>
  );
}
