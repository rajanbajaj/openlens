import type { ButtonHTMLAttributes, ReactNode } from 'react';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual weight. Defaults to `"secondary"`. */
  variant?: ButtonVariant;
  children: ReactNode;
}

const VARIANT_UTILITIES: Record<ButtonVariant, string> = {
  primary:
    'bg-ol-accent text-ol-on-accent hover:bg-ol-accent-strong disabled:cursor-not-allowed disabled:opacity-55',
  secondary:
    'bg-ol-well border-ol-line text-ol-ink hover:border-ol-glass-border hover:shadow-ol disabled:cursor-not-allowed disabled:opacity-55',
  ghost:
    'bg-transparent text-ol-accent-strong px-ol-xs hover:underline disabled:cursor-not-allowed disabled:opacity-55',
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
