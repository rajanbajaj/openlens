import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

/** Every backend endpoint fails: the shell must still render. */
function stubFetchAllFailing(): void {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('backend down')));
}

describe('admin shell', () => {
  it('renders the token bar and all section tabs', () => {
    stubFetchAllFailing();
    render(<App />);
    expect(screen.getByTestId('frontier-admin-token')).toBeInTheDocument();
    expect(screen.getByRole('tablist', { name: 'Admin sections' })).toBeInTheDocument();
    for (const tab of [
      'frontier',
      'analytics',
      'models',
      'providers',
      'ranking',
      'query',
      'plugins',
      'indexing',
    ]) {
      const button = screen.getByTestId(`admin-tab-${tab}`);
      expect(button).toHaveAttribute('role', 'tab');
      expect(button).toBeInTheDocument();
    }
  });

  it('marks the active tab selected and moves with arrow keys', () => {
    stubFetchAllFailing();
    render(<App />);
    const frontier = screen.getByTestId('admin-tab-frontier');
    expect(frontier).toHaveAttribute('aria-selected', 'true');
    fireEvent.keyDown(screen.getByRole('tablist'), { key: 'ArrowRight' });
    expect(screen.getByTestId('admin-tab-analytics')).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('admin-tab-analytics')).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('tablist'), { key: 'Home' });
    expect(screen.getByTestId('admin-tab-frontier')).toHaveAttribute('aria-selected', 'true');
    fireEvent.keyDown(screen.getByRole('tablist'), { key: 'End' });
    expect(screen.getByTestId('admin-tab-indexing')).toHaveAttribute('aria-selected', 'true');
  });

  it('persists the admin token to sessionStorage and forgets it on demand', () => {
    stubFetchAllFailing();
    render(<App />);
    fireEvent.change(screen.getByTestId('frontier-admin-token'), { target: { value: 'secret' } });
    expect(sessionStorage.getItem('openlens-admin-token')).toBe('secret');
    expect(localStorage.getItem('openlens-admin-token')).toBeNull();
    fireEvent.click(screen.getByTestId('frontier-admin-forget'));
    expect(screen.getByTestId('frontier-admin-token')).toHaveValue('');
    expect(sessionStorage.getItem('openlens-admin-token')).toBeNull();
  });

  it('shows an error panel instead of defaults when the frontier snapshot fails', async () => {
    stubFetchAllFailing();
    render(<App />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-snapshot-error')).toBeInTheDocument();
    });
  });

  it('shows file defaults with a pending notice on the models tab', async () => {
    stubFetchAllFailing();
    render(<App />);
    fireEvent.click(screen.getByTestId('admin-tab-models'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent(/pending/i);
    });
    const area = screen.getByTestId('config-editor-models-text') as HTMLTextAreaElement;
    expect(area.value).toContain('fake');
  });
});
