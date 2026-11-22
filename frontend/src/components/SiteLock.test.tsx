import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import SiteLock from './SiteLock.tsx';

afterEach(() => {
  cleanup();
});

describe('SiteLock', () => {
  it('submits the typed password for verification', async () => {
    const onUnlock = vi.fn(async () => true);
    render(<SiteLock onUnlock={onUnlock} />);
    fireEvent.change(screen.getByTestId('site-password'), { target: { value: 's3cret' } });
    fireEvent.click(screen.getByTestId('site-unlock'));
    await waitFor(() => expect(onUnlock).toHaveBeenCalledWith('s3cret'));
  });

  it('shows an inline error when the password is wrong', async () => {
    const onUnlock = vi.fn(async () => false);
    render(<SiteLock onUnlock={onUnlock} />);
    fireEvent.change(screen.getByTestId('site-password'), { target: { value: 'nope' } });
    fireEvent.click(screen.getByTestId('site-unlock'));
    await waitFor(() => expect(screen.getByTestId('site-password-error')).toBeInTheDocument());
  });

  it('keeps the unlock button disabled until a password is typed', () => {
    const onUnlock = vi.fn(async () => true);
    render(<SiteLock onUnlock={onUnlock} />);
    expect(screen.getByTestId('site-unlock')).toBeDisabled();
    fireEvent.change(screen.getByTestId('site-password'), { target: { value: 'x' } });
    expect(screen.getByTestId('site-unlock')).not.toBeDisabled();
  });
});
