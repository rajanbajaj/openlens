import type { ButtonHTMLAttributes, ReactNode } from 'react';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual weight. Defaults to `"secondary"`. */
  variant?: ButtonVariant;
  children: ReactNode;
}

const VARIANT_UTILITIES: Record<ButtonVariant, string> = {
  primary:
    'bg-gradient-to-br from-[#6d28d9] to-[#2563eb] text-ol-on-accent shadow-[0_4px_16px_rgb(37_99_235/0.4)] transition-transform transition-shadow duration-150 hover:-translate-y-px hover:shadow-[0_6px_22px_rgb(37_99_235/0.5)] disabled:cursor-not-allowed disabled:opacity-55',
  secondary:
    'bg-white/45 border-ol-glass-border text-ol-ink hover:bg-white/60 disabled:cursor-not-allowed disabled:opacity-55',
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
