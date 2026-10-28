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
    .map((part) => part.trim())
    .filter((part) => part !== '');
}

/**
 * Federation preferences (T13): include/exclude sets plus an optional
 * primary provider. Suggestions come only from providers the backend
 * has actually reported — the UI never invents provider names.
 * Rendered as a sidebar section; labels and the datalist id are stable.
 */
export function ProviderPrefsControl({
  value,
  onChange,
  knownProviders,
  disabled = false,
}: ProviderPrefsControlProps) {
  return (
    <section aria-label="Provider preferences">
      <h2>Providers</h2>
      <TextInput
        label="Only these providers (comma-separated)"
        id="prefs-include"
        name="prefs_include"
        placeholder="e.g. custom_index"
        value={value.include.join(', ')}
        disabled={disabled}
        list="prefs-known-providers"
        onChange={(event) => onChange({ ...value, include: parseList(event.target.value) })}
      />
      <TextInput
        label="Exclude these providers (comma-separated)"
        id="prefs-exclude"
        name="prefs_exclude"
        value={value.exclude.join(', ')}
        disabled={disabled}
        list="prefs-known-providers"
        onChange={(event) => onChange({ ...value, exclude: parseList(event.target.value) })}
      />
      <TextInput
        label="Primary provider (optional)"
        id="prefs-primary"
        name="prefs_primary"
        value={value.primary ?? ''}
        disabled={disabled}
        list="prefs-known-providers"
        onChange={(event) => onChange({ ...value, primary: event.target.value.trim() || null })}
      />
      <datalist id="prefs-known-providers">
        {knownProviders.map((provider) => (
          <option key={provider} value={provider} />
        ))}
      </datalist>
    </section>
  );
}
