import type { InputHTMLAttributes } from 'react';

export interface TextInputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** Visible label text rendered above the input. */
  label: string;
}

/** Labelled text input bound to the design tokens. */
export function TextInput({ label, id, className, ...rest }: TextInputProps) {
  return (
    <label className="ol-field ol-field-grow" htmlFor={id}>
      {label}
      <input id={id} className={`ol-input${className ? ` ${className}` : ''}`} {...rest} />
    </label>
  );
}
