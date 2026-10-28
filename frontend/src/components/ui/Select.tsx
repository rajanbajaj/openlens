import type { SelectHTMLAttributes } from 'react';

export interface SelectOption {
  value: string;
  label: string;
}

export interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, 'onChange'> {
  /** Visible label text rendered above the select. */
  label: string;
  /** Choices rendered in order. */
  options: SelectOption[];
  /** Called with the newly selected value. */
  onChange: (value: string) => void;
}

/** Labelled native select; always keyboard-operable by construction. */
export function Select({ label, options, onChange, ...rest }: SelectProps) {
  return (
    <label className="ol-field">
      {label}
      <select
        className="ol-select"
        aria-label={label}
        onChange={(event: React.ChangeEvent<HTMLSelectElement>) => {
          onChange(event.target.value);
        }}
        {...rest}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}
