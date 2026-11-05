export interface FieldErrorProps {
  /** Human-readable error text. */
  message: string;
  /** Id referenced by the input's `aria-describedby`. */
  id?: string;
}

/**
 * Field-coupled error text. Pair with `aria-invalid` on the input and
 * `aria-describedby` pointing at this id.
 */
export function FieldError({ message, id }: FieldErrorProps) {
  return (
    <p id={id} data-testid={id ?? 'field-error'} className="ol-field-error">
      {message}
    </p>
  );
}
