import type { ButtonHTMLAttributes, ReactNode } from 'react';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual weight. Defaults to `"secondary"`. */
  variant?: ButtonVariant;
  children: ReactNode;
}

/** Design-system button: primary action, bordered secondary, or inline ghost. */
export function Button({ variant = 'secondary', type = 'button', children, ...rest }: ButtonProps) {
  return (
    <button type={type} className={`ol-button ol-button-${variant}`} {...rest}>
      {children}
    </button>
  );
}
