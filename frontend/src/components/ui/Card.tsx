import type { HTMLAttributes, ReactNode } from 'react';

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode;
}

/** Bordered content card for results and grouped surfaces. */
export function Card({ children, ...rest }: CardProps) {
  return (
    <div className="ol-card" {...rest}>
      {children}
    </div>
  );
}
