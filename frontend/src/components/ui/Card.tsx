import type { HTMLAttributes, ReactNode } from 'react';

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode;
}

/** Flat content container for results and grouped surfaces. */
export function Card({ children, ...rest }: CardProps) {
  return (
    <div className="ol-card rounded-ol-m bg-white px-ol-m py-ol-m" {...rest}>
      {children}
    </div>
  );
}
