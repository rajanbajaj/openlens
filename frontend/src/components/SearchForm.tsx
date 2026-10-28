import { MAX_QUERY_LENGTH, validateQuery } from '../api/client.ts';
import { Button } from './ui/Button.tsx';
import { TextInput } from './ui/TextInput.tsx';

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
}

/**
 * Query input with client-side validation (non-blank, ≤1000 chars).
 * Validation failures render inline and never reach `onSubmit`.
 * Mounted once per page — either in the empty-state hero or in the
 * post-search sticky header — so its input/error ids stay unique.
 */
export default function SearchForm({
  value,
  onChange,
  onSubmit,
  externalError = null,
  disabled = false,
  className,
}: SearchFormProps) {
  const validationError = validateQuery(value);
  const error = (value.length === 0 ? null : validationError) ?? externalError;
  const invalid = error !== null;

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const message = validateQuery(value);
    if (message !== null) return;
    onSubmit(value);
  };

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
        aria-invalid={invalid}
        aria-describedby={invalid ? 'openlens-query-error' : undefined}
        onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
          onChange(event.target.value);
        }}
        disabled={disabled}
      />
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
