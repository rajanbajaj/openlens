import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { readAgentTrace } from '../api/guard.ts';
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

const FULL_TRACE = {
  rewritten_query: 'cheap bluetooth headphones',
  filters: { max_price: 50, exclude_sponsored: true },
  steps: [
    { kind: 'rewrite', detail: 'expanded with bluetooth' },
    { kind: 'retrieve', detail: 'queried 3 providers' },
  ],
  fallbacks: ['provider mock timed out, used cache'],
  model_calls: 2,
};

/**
 * Render the rail with a raw `metadata.agent_trace` value narrowed
 * through the production `readAgentTrace` path, then expand.
 */
function renderOpenWithTrace(diagnostics: Diagnostics, rawTrace: unknown) {
  const agentTrace = readAgentTrace({ metadata: { agent_trace: rawTrace } });
  render(<DiagnosticsDrawer diagnostics={diagnostics} agentTrace={agentTrace} />);
  fireEvent.click(screen.getByRole('button', { name: /show diagnostics/i }));
}

describe('DiagnosticsDrawer agent trace', () => {
  it('renders the full trace when the backend reports one', () => {
    renderOpenWithTrace(FULL_DIAGNOSTICS, FULL_TRACE);

    expect(screen.getByTestId('diagnostics-agent-trace')).toBeInTheDocument();
    expect(screen.getByTestId('diagnostics-agent-rewritten')).toHaveTextContent(
      'cheap bluetooth headphones',
    );
    const steps = screen.getByTestId('diagnostics-agent-steps');
    expect(steps).toHaveTextContent('rewrite');
    expect(steps).toHaveTextContent('expanded with bluetooth');
    expect(steps).toHaveTextContent('retrieve');
    expect(screen.getByTestId('diagnostics-agent-fallbacks')).toHaveTextContent(
      'provider mock timed out, used cache',
    );
    expect(screen.getByTestId('diagnostics-agent-model-calls')).toHaveTextContent('Model calls: 2');
    const filters = screen.getByTestId('diagnostics-agent-filters');
    expect(filters).toHaveTextContent('max_price');
    expect(filters).toHaveTextContent('50');
  });

  it('renders nothing trace-related when the trace is absent (non-agent modes)', () => {
    renderOpen(SPARSE_DIAGNOSTICS);

    expect(screen.queryByTestId('diagnostics-agent-trace')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-agent-rewritten')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-agent-steps')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-agent-fallbacks')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-agent-model-calls')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-agent-filters')).not.toBeInTheDocument();
  });

  it('renders only the reported fields on a partial trace', () => {
    renderOpenWithTrace(SPARSE_DIAGNOSTICS, {
      rewritten_query: 'cheap bluetooth headphones',
      model_calls: 0,
    });

    expect(screen.getByTestId('diagnostics-agent-trace')).toBeInTheDocument();
    expect(screen.getByTestId('diagnostics-agent-rewritten')).toHaveTextContent(
      'cheap bluetooth headphones',
    );
    expect(screen.getByTestId('diagnostics-agent-model-calls')).toHaveTextContent('Model calls: 0');
    expect(screen.queryByTestId('diagnostics-agent-steps')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-agent-fallbacks')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-agent-filters')).not.toBeInTheDocument();
  });

  it('renders a trace with only steps and degrades malformed entries', () => {
    renderOpenWithTrace(SPARSE_DIAGNOSTICS, {
      steps: [{ kind: 'retrieve' }, { detail: 'no kind here' }, {}, 42, 'nope'],
    });

    const steps = screen.getByTestId('diagnostics-agent-steps');
    expect(steps).toHaveTextContent('retrieve');
    expect(steps).toHaveTextContent('no kind here');
    expect(screen.queryByTestId('diagnostics-agent-rewritten')).not.toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-agent-fallbacks')).not.toBeInTheDocument();
  });

  it('treats empty or malformed traces as absent', () => {
    for (const trace of [{}, { steps: [], fallbacks: [] }, 'trace', 42, { steps: [{}] }]) {
      cleanup();
      renderOpenWithTrace(SPARSE_DIAGNOSTICS, trace);
      expect(screen.queryByTestId('diagnostics-agent-trace')).not.toBeInTheDocument();
    }
  });
});
