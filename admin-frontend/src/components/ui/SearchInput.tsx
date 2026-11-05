import { useEffect, useId, useState } from 'react';

export interface SearchInputProps {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  ariaLabel: string;
  /** Debounce before propagating edits. Defaults to 150ms; 0 = sync. */
  debounceMs?: number;
}

/**
 * Labeled search field with a built-in clear button. Edits propagate
 * through a short debounce; the clear button resets synchronously.
 */
export function SearchInput({
  value,
  onChange,
  placeholder,
  ariaLabel,
  debounceMs = 150,
}: SearchInputProps) {
  const [draft, setDraft] = useState(value);
  const inputId = useId();

  useEffect(() => {
    setDraft(value);
  }, [value]);

  useEffect(() => {
    if (debounceMs <= 0 || draft === value) return;
    const timer = setTimeout(() => onChange(draft), debounceMs);
    return () => clearTimeout(timer);
  }, [draft, debounceMs, onChange, value]);

  function handleChange(next: string): void {
    setDraft(next);
    if (debounceMs <= 0) onChange(next);
  }

  return (
    <span className="inline-flex items-center gap-ol-xs" data-testid="search-input">
      <label htmlFor={inputId} className="ol-sr-only">
        {ariaLabel}
      </label>
      <input
        id={inputId}
        data-testid="search-input-field"
        type="search"
        value={draft}
        placeholder={placeholder}
        aria-label={ariaLabel}
        onChange={(event) => handleChange(event.target.value)}
        className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
      />
      {draft !== '' ? (
        <button
          type="button"
          data-testid="search-input-clear"
          aria-label={`Clear ${ariaLabel}`}
          onClick={() => handleChange('')}
          className="shrink-0 cursor-pointer rounded-ol-s px-ol-xs text-ol-ink-soft underline"
        >
          ×
        </button>
      ) : null}
    </span>
  );
}
