import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import type { ComponentType } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.useRealTimers();
  vi.resetModules();
});

interface SearchFormProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: (query: string) => void;
}

/** Import SearchForm with suggestions forced off (the demo default). */
async function importSuggestOff(): Promise<ComponentType<SearchFormProps>> {
  vi.stubEnv('VITE_ENABLE_SUGGEST', '');
  const mod = await import('./SearchForm.tsx');
  return mod.default as ComponentType<SearchFormProps>;
}

function Harness({
  Form,
  onSubmit,
}: {
  Form: ComponentType<SearchFormProps>;
  onSubmit: (query: string) => void;
}) {
  const [value, setValue] = useState('');
  return <Form value={value} onChange={setValue} onSubmit={onSubmit} />;
}

describe('SearchForm suggestions kill switch', () => {
  it('issues no suggest fetch and shows no dropdown while typing', async () => {
    const Form = await importSuggestOff();
    vi.useFakeTimers();
    const fetchStub = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve([]),
    });
    vi.stubGlobal('fetch', fetchStub);
    render(<Harness Form={Form} onSubmit={() => {}} />);

    fireEvent.change(screen.getByLabelText(/search query/i), {
      target: { value: 'dating advice' },
    });
    vi.runAllTimers();

    expect(fetchStub).not.toHaveBeenCalled();
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('still submits the typed query directly', async () => {
    const Form = await importSuggestOff();
    const onSubmit = vi.fn();
    render(<Harness Form={Form} onSubmit={onSubmit} />);

    fireEvent.change(screen.getByLabelText(/search query/i), {
      target: { value: 'dating advice' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }));

    expect(onSubmit).toHaveBeenCalledWith('dating advice');
  });
});
