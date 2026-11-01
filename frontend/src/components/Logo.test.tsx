import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Logo } from './Logo.tsx';

describe('Logo', () => {
  it('renders the open-ring mark and the two-weight wordmark', () => {
    render(<Logo size="header" />);
    expect(screen.getByRole('img', { name: 'OpenLens mark' })).toBeInTheDocument();
    expect(screen.getByText('Open')).toBeInTheDocument();
    expect(screen.getByText('Lens')).toBeInTheDocument();
  });
});
