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
  /** Extra class on the wrapper for hero vs compact-header contexts. */
  className?: string;
}

function isSearchMode(value: string): value is SearchMode {
  return (SEARCH_MODES as string[]).includes(value);
}

/**
 * Mode selector for the search request (`default`, `child-safe`,
 * `news`, `shopping`, `travel`). Controlled by the parent, which
 * forwards the selection as `mode` in the request body. Mounted once
 * per page — either in the empty-state hero or in the post-search
 * sticky header, reusing the same component.
 */
export default function ModeSelector({
  mode,
  onChange,
  disabled = false,
  className,
}: ModeSelectorProps) {
  return (
    <div className={className}>
      <Select
        label="Search mode"
        value={mode}
        disabled={disabled}
        options={SEARCH_MODES.map((option) => ({ value: option, label: option }))}
        onChange={(value) => {
          if (isSearchMode(value)) onChange(value);
        }}
      />
    </div>
  );
}
