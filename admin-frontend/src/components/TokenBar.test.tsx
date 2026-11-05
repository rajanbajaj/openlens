import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TokenBar } from './TokenBar.tsx';

afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

describe('TokenBar', () => {
  it('separates the hint from the label and toggles visibility', () => {
    const onChange = vi.fn();
    render(<TokenBar adminToken="secret" onChange={onChange} />);
    expect(screen.getByText('Admin token')).toBeInTheDocument();
    const field = screen.getByTestId('frontier-admin-token');
    expect(field).toHaveAttribute('type', 'password');
    const toggle = screen.getByTestId('frontier-admin-token-visibility');
    expect(toggle).toHaveTextContent('Show token');
    expect(toggle).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(toggle);
    expect(field).toHaveAttribute('type', 'text');
    expect(toggle).toHaveTextContent('Hide token');
    expect(toggle).toHaveAttribute('aria-pressed', 'true');
  });

  it('forgets through the design-system button', () => {
    const onChange = vi.fn();
    render(<TokenBar adminToken="secret" onChange={onChange} />);
    const forget = screen.getByTestId('frontier-admin-forget');
    expect(forget.className).toContain('ol-button-ghost');
    fireEvent.click(forget);
    expect(onChange).toHaveBeenCalledWith('');
  });
});
