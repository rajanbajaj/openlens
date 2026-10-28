/**
 * Suggestion dropdown for as-you-type completions.
 *
 * Presentational listbox: the debounce timer, fetching, and abort of
 * stale requests live in `SearchForm.tsx` — this component only renders
 * what it is given. It renders nothing (silence, not an empty box) when
 * there are no suggestions and no fetch is in flight, so a
 * backend-absent or malformed suggest payload degrades to direct search.
 *
 * Motion and chrome reuse existing token classes only (`ol-card` for the
 * panel, `ol-chip`/`ol-badge` for idle/highlighted options,
 * `ol-result-count` for the loading line, `ol-sr-only` for the live
 * announcer, `ol-transition-fade` for the enter fade). `ol-transition-fade`
 * applies its transition only under
 * `prefers-reduced-motion: no-preference`, so the dropdown is instant for
 * reduced-motion users with no extra CSS.
 */

export interface SuggestDropdownProps {
  /** Completions to offer, in backend order. Empty hides the list. */
  suggestions: string[];
  /** Highlighted option index, or -1 when nothing is highlighted. */
  activeIndex?: number;
  /** True while a suggest fetch is in flight; shows the loading line. */
  loading?: boolean;
  /** `id` of the listbox, referenced by the input's `aria-controls`. */
  listId: string;
  /** Called when the user picks a suggestion (click or Enter). */
  onSelect: (suggestion: string) => void;
  /** Called on pointer hover so the mouse and keyboard share one highlight. */
  onActiveChange?: (index: number) => void;
}

/**
 * Next highlight for an ArrowUp/ArrowDown step over `count` options.
 * Wraps at both ends; `-1` means "nothing highlighted", from which
 * ArrowDown lands on the first option and ArrowUp on the last.
 */
export function moveSuggestHighlight(
  current: number,
  key: 'ArrowDown' | 'ArrowUp',
  count: number,
): number {
  if (count <= 0) return -1;
  if (key === 'ArrowDown') {
    return current < 0 ? 0 : (current + 1) % count;
  }
  return current < 0 ? count - 1 : (current - 1 + count) % count;
}

export default function SuggestDropdown({
  suggestions,
  activeIndex = -1,
  loading = false,
  listId,
  onSelect,
  onActiveChange,
}: SuggestDropdownProps) {
  // Empty-suggest silence: no box, no announcer, no loading chrome.
  if (!loading && suggestions.length === 0) return null;

  const announcement =
    loading && suggestions.length === 0
      ? 'Loading suggestions…'
      : `${suggestions.length} suggestion${suggestions.length === 1 ? '' : 's'} available`;

  return (
    <div data-testid="suggest-dropdown" className="ol-card ol-transition-fade">
      <p aria-live="polite" data-testid="suggest-announcer" className="ol-sr-only">
        {announcement}
      </p>
      {loading && suggestions.length === 0 ? (
        <p data-testid="suggest-loading" className="ol-result-count">
          Loading suggestions…
        </p>
      ) : null}
      {suggestions.length > 0 || loading ? (
        <ul role="listbox" id={listId} data-testid="suggest-list" aria-label="Search suggestions">
          {suggestions.map((suggestion, index) => (
            <li
              key={`${index}-${suggestion}`}
              role="option"
              id={`${listId}-option-${index}`}
              aria-selected={index === activeIndex}
              data-testid={`suggest-option-${index}`}
              className={index === activeIndex ? 'ol-chip' : 'ol-badge'}
              onMouseDown={(event: React.MouseEvent<HTMLLIElement>) => {
                // Beat input blur so the click selects before the list closes.
                event.preventDefault();
                onSelect(suggestion);
              }}
              onMouseEnter={() => {
                onActiveChange?.(index);
              }}
            >
              {suggestion}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
