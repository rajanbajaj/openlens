import type { InputHTMLAttributes } from 'react';

export interface TextInputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** Visible label text rendered above the input. */
  label: string;
}

/** Labelled text input bound to the design tokens. */
export function TextInput({ label, id, className, ...rest }: TextInputProps) {
  return (
    <label
      className="ol-field ol-field-grow flex flex-col gap-ol-xs text-ol-s font-semibold"
      htmlFor={id}
    >
      {label}
      <input
        id={id}
        className={`ol-input font-normal text-ol-ink bg-white border border-ol-line rounded-ol-m px-ol-s py-ol-m disabled:bg-ol-well disabled:text-ol-ink-soft${className ? ` ${className}` : ''}`}
        {...rest}
      />
    </label>
  );
}
