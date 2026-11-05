import type { ButtonHTMLAttributes, ReactNode } from 'react';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'destructive';

export type ButtonSize = 'md' | 'sm';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual weight. Defaults to `"secondary"`. */
  variant?: ButtonVariant;
  /** Density. `"sm"` keeps the 32px minimum hit target. Defaults to `"md"`. */
  size?: ButtonSize;
  children: ReactNode;
}

const VARIANT_UTILITIES: Record<ButtonVariant, string> = {
  primary:
    'bg-ol-accent text-ol-on-accent transition-colors duration-150 hover:bg-ol-accent-strong disabled:cursor-not-allowed disabled:opacity-55',
  secondary:
    'bg-white border-ol-glass-border text-ol-ink hover:bg-ol-well disabled:cursor-not-allowed disabled:opacity-55',
  ghost:
    'bg-transparent text-ol-accent-strong underline px-ol-xs disabled:cursor-not-allowed disabled:opacity-55',
  destructive:
    'bg-ol-error text-white transition-colors duration-150 hover:brightness-95 disabled:cursor-not-allowed disabled:opacity-55',
};

const SIZE_UTILITIES: Record<ButtonSize, string> = {
  md: 'px-ol-s py-ol-s',
  sm: 'px-ol-s py-ol-xs min-h-[2rem]',
};

/** Design-system button: primary action, bordered secondary, inline ghost. */
export function Button({
  variant = 'secondary',
  size = 'md',
  type = 'button',
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={`ol-button ol-button-${variant} text-ol-s font-semibold rounded-ol-s border border-transparent cursor-pointer ${VARIANT_UTILITIES[variant]} ${SIZE_UTILITIES[size]}`}
      {...rest}
    >
      {children}
    </button>
  );
}
