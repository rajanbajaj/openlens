import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
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
    for (const tab of ['frontier', 'models', 'providers', 'ranking', 'query', 'plugins']) {
      expect(screen.getByTestId(`admin-tab-${tab}`)).toBeInTheDocument();
    }
  });

  it('persists the admin token to localStorage', () => {
    stubFetchAllFailing();
    render(<App />);
    fireEvent.change(screen.getByTestId('frontier-admin-token'), { target: { value: 'secret' } });
    expect(localStorage.getItem('openlens-admin-token')).toBe('secret');
  });

  it('shows the frontier dashboard on the default tab', async () => {
    stubFetchAllFailing();
    render(<App />);
    await waitFor(() => {
      expect(screen.getByTestId('frontier-state')).toBeInTheDocument();
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
