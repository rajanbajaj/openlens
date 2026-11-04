import { useEffect, useRef, useState } from 'react';
import { FILTER_DEBOUNCE_MS, sanitizeInput } from '../api/client.ts';
import type { ProviderPrefs } from '../api/types.ts';
import { TextInput } from './ui/TextInput.tsx';

export interface ProviderPrefsControlProps {
  /** Current prefs; null primary means no preference. */
  value: ProviderPrefs;
  /** Called with the next prefs on every edit. */
  onChange: (next: ProviderPrefs) => void;
  /** Provider names seen in this session, offered as suggestions. */
  knownProviders: string[];
  /** Disables every field while a search is in flight. */
  disabled?: boolean;
}

function parseList(raw: string): string[] {
  return raw
    .split(',')
    .map((part) => sanitizeInput(part))
    .filter((part) => part !== '');
}

interface PrefsDraft {
  include: string;
  exclude: string;
  primary: string;
}

function toDraft(value: ProviderPrefs): PrefsDraft {
  return {
    include: value.include.join(', '),
    exclude: value.exclude.join(', '),
    primary: value.primary ?? '',
  };
}

function fromDraft(draft: PrefsDraft): ProviderPrefs {
  return {
    include: parseList(draft.include),
    exclude: parseList(draft.exclude),
    primary: draft.primary.trim() || null,
  };
}

function keyOf(prefs: ProviderPrefs): string {
  return JSON.stringify([prefs.include, prefs.exclude, prefs.primary]);
}

/**
 * Federation preferences (T13): include/exclude sets plus an optional
 * primary provider. Suggestions come only from providers the backend
 * has actually reported — the UI never invents provider names.
 * Rendered as a sidebar section; labels and the datalist id are stable.
 *
 * The text fields edit a local draft so typing stays instant, and the
 * parsed prefs propagate to the parent after FILTER_DEBOUNCE_MS of
 * quiet. Without the delay every keystroke would re-run the search
 * through the parent handler; discrete edits still resolve on their
 * own keystroke because the draft already matches when the user
 * commits a single change. The draft re-syncs whenever the parent
 * value changes from elsewhere (e.g. a filter reset).
 */
export function ProviderPrefsControl({
  value,
  onChange,
  knownProviders,
  disabled = false,
}: ProviderPrefsControlProps) {
  const [draft, setDraft] = useState<PrefsDraft>(() => toDraft(value));
  const parentKey = keyOf(value);
  const [syncedKey, setSyncedKey] = useState(parentKey);
  if (parentKey !== syncedKey) {
    setSyncedKey(parentKey);
    setDraft(toDraft(value));
  }

  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    if (keyOf(fromDraft(draft)) === parentKey) return;
    const timer = window.setTimeout(() => {
      onChangeRef.current(fromDraft(draft));
    }, FILTER_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [draft, parentKey]);

  return (
    <section aria-label="Provider preferences">
      <h2>Providers</h2>
      <TextInput
        label="Only these providers (comma-separated)"
        id="prefs-include"
        name="prefs_include"
        placeholder="e.g. custom_index"
        value={draft.include}
        disabled={disabled}
        list="prefs-known-providers"
        onChange={(event) => setDraft({ ...draft, include: event.target.value })}
      />
      <TextInput
        label="Exclude these providers (comma-separated)"
        id="prefs-exclude"
        name="prefs_exclude"
        value={draft.exclude}
        disabled={disabled}
        list="prefs-known-providers"
        onChange={(event) => setDraft({ ...draft, exclude: event.target.value })}
      />
      <TextInput
        label="Primary provider (optional)"
        id="prefs-primary"
        name="prefs_primary"
        value={draft.primary}
        disabled={disabled}
        list="prefs-known-providers"
        onChange={(event) => setDraft({ ...draft, primary: event.target.value })}
      />
      <datalist id="prefs-known-providers">
        {knownProviders.map((provider) => (
          <option key={provider} value={provider} />
        ))}
      </datalist>
    </section>
  );
}
