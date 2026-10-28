import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type { Diagnostics } from '../api/types.ts';
import { DiagnosticsDrawer } from './DiagnosticsDrawer.tsx';

afterEach(() => {
  cleanup();
});

const FULL_DIAGNOSTICS: Diagnostics = {
  fallback_used: true,
  provider_latencies_ms: { opensearch: 42, mock: 12.5 },
  provider_result_counts: { opensearch: 8, mock: 2 },
  provider_costs_estimated: { opensearch: 0.004 },
  plugin_executions: [
    { plugin: 'retriever', stage: 'retrieve', status: 'ok', latency_ms: 30 },
    { plugin: 'reranker', stage: 'rerank', status: 'error', error: 'timeout' },
  ],
  errors: ['provider opensearch timed out'],
  query: {
    normalized: 'wireless headphones',
    truncated: false,
    intent: 'shopping',
    suggested_mode: 'shopping',
    explicit_mode: 'default',
    effective_mode: 'default',
    base_terms: ['wireless', 'headphones'],
    added_terms: ['bluetooth'],
    provenance: { intent: 'rule' },
    reasons: ['keyword match'],
  },
  request_id: 'req-123',
  cache: { status: 'hit' },
};

const SPARSE_DIAGNOSTICS: Diagnostics = {
  fallback_used: false,
  provider_latencies_ms: {},
  plugin_executions: [],
  errors: [],
};

/** Render the rail and expand the drawer, returning nothing. */
function renderOpen(diagnostics: Diagnostics) {
  render(<DiagnosticsDrawer diagnostics={diagnostics} />);
  fireEvent.click(screen.getByRole('button', { name: /show diagnostics/i }));
}

describe('DiagnosticsDrawer rail', () => {
  it('renders the frosted rail with the full timeline when open', () => {
    renderOpen(FULL_DIAGNOSTICS);

    expect(screen.getByTestId('diagnostics-panel')).toBeInTheDocument();
    expect(screen.getByTestId('diagnostics-fallback')).toHaveTextContent('Degraded: fallback used');
    expect(screen.getByTestId('diagnostics-request-id')).toHaveTextContent('req-123');

    const latencies = screen.getByTestId('diagnostics-latencies');
    expect(latencies).toHaveTextContent('opensearch');
    expect(latencies).toHaveTextContent('42 ms');
    expect(latencies).toHaveTextContent('8 results');
    expect(latencies).toHaveTextContent('est. cost 0.004');

    const plugins = screen.getByTestId('diagnostics-plugins');
    expect(plugins).toHaveTextContent('retriever');
    expect(plugins).toHaveTextContent('rerank');
    expect(plugins).toHaveTextContent('30 ms');
    expect(plugins).toHaveTextContent('error: timeout');

    const query = screen.getByTestId('diagnostics-query');
    expect(query).toHaveTextContent('shopping');
    expect(query).toHaveTextContent('default');
    expect(query).toHaveTextContent('(suggested shopping)');
    expect(query).toHaveTextContent('bluetooth');

    expect(screen.getByTestId('diagnostics-errors')).toHaveTextContent(
      'provider opensearch timed out',
    );
    expect(screen.getByTestId('diagnostics-developer')).toHaveTextContent('Developer details');
    expect(screen.getByTestId('diagnostics-cache')).toHaveTextContent('Cache hit');
  });

  it('keeps the copyable request id working without a clipboard', () => {
    renderOpen(FULL_DIAGNOSTICS);
    fireEvent.click(screen.getByRole('button', { name: /^copy$/i }));
    expect(screen.getByTestId('diagnostics-request-id')).toBeInTheDocument();
  });

  it('degrades gracefully on sparse diagnostics', () => {
    renderOpen(SPARSE_DIAGNOSTICS);

    expect(screen.getByTestId('diagnostics-fallback')).toHaveTextContent('Live');
    expect(screen.getByText('No provider latencies reported')).toBeInTheDocument();
    expect(screen.getByText('No plugin executions')).toBeInTheDocument();
    expect(screen.getByText('No errors')).toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-request-id')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-query')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-latencies')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-plugins')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-errors')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-cache')).not.toBeInTheDocument();
    expect(screen.getByTestId('diagnostics-developer')).toBeInTheDocument();
  });

  it.each([
    ['hit', 'Cache hit'],
    ['miss', 'Cache miss'],
    ['error', 'Cache error'],
  ])('badges cache status %s', (status, label) => {
    renderOpen({ ...SPARSE_DIAGNOSTICS, cache: { status } });
    expect(screen.getByTestId('diagnostics-cache')).toHaveTextContent(label);
  });

  it('renders no cache badge when the backend reports no cache key', () => {
    renderOpen(SPARSE_DIAGNOSTICS);
    expect(screen.queryByTestId('diagnostics-cache')).not.toBeInTheDocument();
  });

  it('renders no cache badge for empty or unknown statuses', () => {
    const { rerender } = render(
      <DiagnosticsDrawer diagnostics={{ ...SPARSE_DIAGNOSTICS, cache: {} }} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /show diagnostics/i }));
    expect(screen.queryByTestId('diagnostics-cache')).not.toBeInTheDocument();

    rerender(
      <DiagnosticsDrawer diagnostics={{ ...SPARSE_DIAGNOSTICS, cache: { status: 'stale' } }} />,
    );
    expect(screen.queryByTestId('diagnostics-cache')).not.toBeInTheDocument();
  });

  it('survives malformed optional keys without crashing', () => {
    const malformed = {
      ...SPARSE_DIAGNOSTICS,
      request_id: '   ',
      plugin_executions: [{}, { plugin: '', stage: '', status: '' }],
      cache: { status: 42 },
    } as unknown as Diagnostics;
    renderOpen(malformed);

    expect(screen.queryByTestId('diagnostics-request-id')).not.toBeInTheDocument();
    expect(screen.getByTestId('diagnostics-plugins')).toHaveTextContent('unknown');
    expect(screen.queryByTestId('diagnostics-cache')).not.toBeInTheDocument();
  });
});
