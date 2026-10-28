import type { SearchMode } from '../api/types.ts';
import { Select } from './ui/Select.tsx';

/** Modes offered by the selector, in display order. `default` first. */
export const SEARCH_MODES: SearchMode[] = ['default', 'child-safe', 'news', 'shopping', 'travel'];

export interface ModeSelectorProps {
  /** Currently selected mode. Defaults to `"default"`. */
  mode: SearchMode;
  /** Called with the newly selected mode. */
  onChange: (mode: SearchMode) => void;
  /** Disables the selector while a search is in flight. */
  disabled?: boolean;
}

function isSearchMode(value: string): value is SearchMode {
  return (SEARCH_MODES as string[]).includes(value);
}

/**
 * Mode selector for the search request (`default`, `child-safe`,
 * `news`, `shopping`, `travel`). Controlled by the parent, which
 * forwards the selection as `mode` in the request body.
 */
export default function ModeSelector({ mode, onChange, disabled = false }: ModeSelectorProps) {
  return (
    <Select
      label="Search mode"
      value={mode}
      disabled={disabled}
      options={SEARCH_MODES.map((option) => ({ value: option, label: option }))}
      onChange={(value) => {
        if (isSearchMode(value)) onChange(value);
      }}
    />
  );
}
