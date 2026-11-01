import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import SearchForm from './SearchForm.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderForm(autoFocus = false) {
  return render(
    <SearchForm value="" onChange={() => {}} onSubmit={() => {}} autoFocus={autoFocus} />,
  );
}

describe('SearchForm accessibility', () => {
  it('moves focus into the query input on the landing hero', () => {
    renderForm(true);
    expect(screen.getByLabelText(/search query/i)).toBe(document.activeElement);
  });

  it('leaves focus alone without the hero autofocus flag', () => {
    renderForm(false);
    expect(screen.getByLabelText(/search query/i)).not.toBe(document.activeElement);
  });

  it('hides voice input where speech recognition is unsupported', () => {
    renderForm();
    expect(screen.queryByTestId('voice-input')).toBeNull();
  });
});
