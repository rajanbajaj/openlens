import type { HTMLAttributes, ReactNode } from 'react';

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode;
}

/** Bordered content card for results and grouped surfaces. */
export function Card({ children, ...rest }: CardProps) {
  return (
    <div
      className="ol-card rounded-ol-m shadow-[0_4px_30px_rgb(0_0_0/0.1)] bg-white/55 border border-white/20 px-ol-m py-ol-m backdrop-blur-[8px]"
      {...rest}
    >
      {children}
    </div>
  );
}
