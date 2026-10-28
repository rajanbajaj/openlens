import axe from 'axe-core';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const ENVELOPE = {
  query: 'wireless headphones',
  mode: 'shopping',
  results: [
    {
      id: 'r1',
      title: 'Acme Headphones',
      snippet: 'Great sound',
      original_score: 0.9,
      final_score: 0.95,
      url: 'https://example.com/h',
      provider: 'mock',
      reasons: ['matches query'],
      warnings: ['demo warning'],
      metadata: {},
    },
  ],
  total: 1,
  applied_filters: ['user-restrictions'],
  diagnostics: {
    fallback_used: false,
    provider_latencies_ms: { mock: 12.5 },
    provider_result_counts: { mock: 1 },
    provider_costs_estimated: { mock: 0 },
    plugin_executions: [],
    errors: [],
    request_id: 'abc123',
  },
};

async function runSearchFlow() {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(ENVELOPE) }),
  );
  const rendered = render(<App />);
  fireEvent.change(screen.getByLabelText(/search query/i), {
    target: { value: 'wireless headphones' },
  });
  fireEvent.click(screen.getByRole('button', { name: /^search$/i }));
  await waitFor(() => {
    expect(screen.getByText('Acme Headphones')).toBeInTheDocument();
  });
  fireEvent.click(await screen.findByRole('button', { name: /show diagnostics/i }));
  await waitFor(() => {
    expect(screen.getByTestId('diagnostics-panel')).toBeInTheDocument();
  });
  return rendered;
}

describe('accessibility', () => {
  it('search flow reports zero critical axe violations', async () => {
    const { container } = await runSearchFlow();
    const results = await axe.run(container);
    const critical = results.violations.filter((violation) => violation.impact === 'critical');
    expect(critical).toEqual([]);
  });

  it('Escape closes the open diagnostics drawer', async () => {
    await runSearchFlow();
    const panel = screen.getByTestId('diagnostics-panel');
    fireEvent.keyDown(panel, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByTestId('diagnostics-panel')).not.toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: /show diagnostics/i })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
  });

  it('announces result counts through a live region', async () => {
    await runSearchFlow();
    expect(screen.getByTestId('result-announcer')).toHaveTextContent(
      '1 result for “wireless headphones”',
    );
  });
});
