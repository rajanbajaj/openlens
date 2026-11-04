import type { ButtonHTMLAttributes, ReactNode } from 'react';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual weight. Defaults to `"secondary"`. */
  variant?: ButtonVariant;
  children: ReactNode;
}

const VARIANT_UTILITIES: Record<ButtonVariant, string> = {
  primary:
    'bg-ol-accent text-ol-on-accent transition-colors duration-150 hover:bg-ol-accent-strong disabled:cursor-not-allowed disabled:opacity-55',
  secondary:
    'bg-white border-ol-glass-border text-ol-ink hover:bg-ol-well disabled:cursor-not-allowed disabled:opacity-55',
  ghost:
    'bg-transparent text-ol-accent-strong underline px-ol-xs disabled:cursor-not-allowed disabled:opacity-55',
};

/** Design-system button: primary action, bordered secondary, or inline ghost. */
export function Button({ variant = 'secondary', type = 'button', children, ...rest }: ButtonProps) {
  return (
    <button
      type={type}
      className={`ol-button ol-button-${variant} text-ol-s font-semibold rounded-ol-s border border-transparent px-ol-s py-ol-s cursor-pointer ${VARIANT_UTILITIES[variant]}`}
      {...rest}
    >
      {children}
    </button>
  );
}
