import { useState } from 'react';
import type { AgentTrace, Diagnostics } from '../api/types';
import { summarizePluginExecution } from '../api/enrichments.ts';
import { Badge } from './ui/Badge.tsx';
import { Button } from './ui/Button.tsx';
import { Drawer } from './ui/Drawer.tsx';
import { GlassPanel } from './ui/GlassPanel.tsx';

export interface DiagnosticsDrawerProps {
  diagnostics: Diagnostics;
  /**
   * Narrowed agentic-mode trace (`metadata.agent_trace`, via
   * `readAgentTrace`). Null outside agent mode or when the backend
   * omitted it — the section renders nothing then.
   */
  agentTrace?: AgentTrace | null;
}

/** Copy text to the clipboard; false when the API is unavailable. */
async function copyText(text: string): Promise<boolean> {
  try {
    const clipboard = (navigator as Navigator & { clipboard?: Clipboard }).clipboard;
    if (!clipboard) return false;
    await clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Badge labels for known cache lookup outcomes; anything else renders nothing. */
const CACHE_LABELS: Record<string, string> = {
  hit: 'Cache hit',
  miss: 'Cache miss',
  error: 'Cache error',
};

/** Cache badge label, or null when uncached or the status is unknown. */
function cacheLabel(diagnostics: Diagnostics): string | null {
  const status: unknown = diagnostics.cache?.status;
  if (typeof status !== 'string') return null;
  return CACHE_LABELS[status] ?? null;
}

/**
 * Diagnostics rail: request id (copyable), provider latency / result /
 * cost timeline, plugin executions with stage/status/latency, fallback
 * indicator, cache lookup badge, query provenance, agent trace, and
 * errors. Raw metadata stays behind an explicit developer-details
 * disclosure; every optional key degrades gracefully when the backend
 * omits it.
 */
export function DiagnosticsDrawer({ diagnostics, agentTrace = null }: DiagnosticsDrawerProps) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const providers = Object.keys(diagnostics.provider_latencies_ms).sort();
  const executions = diagnostics.plugin_executions.map(summarizePluginExecution);
  const requestId = diagnostics.request_id?.trim() ? diagnostics.request_id : null;
  const provenance = diagnostics.query;
  const cache = cacheLabel(diagnostics);
  const traceFilters =
    agentTrace?.filters && Object.keys(agentTrace.filters).length > 0
      ? Object.keys(agentTrace.filters).sort()
      : [];

  async function handleCopy() {
    if (!requestId) return;
    setCopied(await copyText(requestId));
  }

  return (
    <GlassPanel variant="rail">
      <Drawer
        triggerLabel={open ? 'Hide diagnostics' : 'Show diagnostics'}
        open={open}
        onToggle={() => setOpen((v) => !v)}
        panelId="diagnostics-panel"
        panelTestId="diagnostics-panel"
      >
        <p data-testid="diagnostics-fallback" className="my-ol-s">
          {diagnostics.fallback_used ? <Badge>Degraded: fallback used</Badge> : <Badge>Live</Badge>}
        </p>
        {cache ? (
          <p data-testid="diagnostics-cache">
            <Badge>{cache}</Badge>
          </p>
        ) : null}
        {requestId ? (
          <p data-testid="diagnostics-request-id">
            Request <code>{requestId}</code>{' '}
            <Button variant="ghost" onClick={() => void handleCopy()}>
              {copied ? 'Copied' : 'Copy'}
            </Button>
          </p>
        ) : null}
        <h4 className="mt-ol-m mb-ol-s text-ol-m">Providers</h4>
        {providers.length === 0 ? (
          <p>No provider latencies reported</p>
        ) : (
          <dl
            data-testid="diagnostics-latencies"
            className="grid grid-cols-[auto_1fr] gap-x-ol-m gap-y-ol-xs m-0"
          >
            {providers.map((provider) => (
              <div key={provider} className="contents">
                <dt>{provider}</dt>
                <dd>
                  {diagnostics.provider_latencies_ms[provider]} ms
                  {diagnostics.provider_result_counts?.[provider] !== undefined
                    ? ` · ${diagnostics.provider_result_counts[provider]} results`
                    : null}
                  {diagnostics.provider_costs_estimated?.[provider] !== undefined
                    ? ` · est. cost ${diagnostics.provider_costs_estimated[provider]}`
                    : null}
                </dd>
              </div>
            ))}
          </dl>
        )}
        <h4 className="mt-ol-m mb-ol-s text-ol-m">Pipeline ({executions.length})</h4>
        {executions.length === 0 ? (
          <p>No plugin executions</p>
        ) : (
          <ol data-testid="diagnostics-plugins" className="flex flex-col gap-ol-xs my-ol-s pl-ol-m">
            {executions.map((execution, index) => (
              <li key={`${execution.plugin}-${index}`}>
                <Badge>{execution.stage}</Badge> {execution.plugin} · {execution.status}
                {execution.latencyMs !== null ? ` · ${execution.latencyMs} ms` : null}
                {execution.error ? ` · error: ${execution.error}` : null}
              </li>
            ))}
          </ol>
        )}
        {provenance ? (
          <>
            <h4 className="mt-ol-m mb-ol-s text-ol-m">Query</h4>
            <dl
              data-testid="diagnostics-query"
              className="grid grid-cols-[auto_1fr] gap-x-ol-m gap-y-ol-xs m-0"
            >
              <div className="contents">
                <dt>Intent</dt>
                <dd>{provenance.intent}</dd>
              </div>
              <div className="contents">
                <dt>Mode</dt>
                <dd>
                  {provenance.effective_mode}
                  {provenance.suggested_mode !== provenance.effective_mode
                    ? ` (suggested ${provenance.suggested_mode})`
                    : null}
                </dd>
              </div>
              {provenance.added_terms.length > 0 ? (
                <div className="contents">
                  <dt>Expanded with</dt>
                  <dd>{provenance.added_terms.join(', ')}</dd>
                </div>
              ) : null}
            </dl>
          </>
        ) : null}
        {agentTrace ? (
          <>
            <h4 className="mt-ol-m mb-ol-s text-ol-m">Agent trace</h4>
            <div data-testid="diagnostics-agent-trace">
              {agentTrace.rewritten_query ? (
                <p data-testid="diagnostics-agent-rewritten" className="my-ol-s">
                  Rewritten query: {agentTrace.rewritten_query}
                </p>
              ) : null}
              {agentTrace.model_calls !== undefined ? (
                <p data-testid="diagnostics-agent-model-calls" className="my-ol-s">
                  Model calls: {agentTrace.model_calls}
                </p>
              ) : null}
              {traceFilters.length > 0 && agentTrace.filters ? (
                <dl
                  data-testid="diagnostics-agent-filters"
                  className="grid grid-cols-[auto_1fr] gap-x-ol-m gap-y-ol-xs m-0 my-ol-s"
                >
                  {traceFilters.map((key) => (
                    <div key={key} className="contents">
                      <dt>{key}</dt>
                      <dd>{JSON.stringify(agentTrace.filters?.[key])}</dd>
                    </div>
                  ))}
                </dl>
              ) : null}
              {agentTrace.steps && agentTrace.steps.length > 0 ? (
                <ol
                  data-testid="diagnostics-agent-steps"
                  className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
                >
                  {agentTrace.steps.map((step, index) => (
                    <li key={`${step.kind ?? 'step'}-${index}`}>
                      {step.kind ? <Badge>{step.kind}</Badge> : null}{' '}
                      {step.detail ?? step.kind ?? 'step'}
                    </li>
                  ))}
                </ol>
              ) : null}
              {agentTrace.fallbacks && agentTrace.fallbacks.length > 0 ? (
                <ul
                  data-testid="diagnostics-agent-fallbacks"
                  className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
                >
                  {agentTrace.fallbacks.map((fallback) => (
                    <li key={fallback}>
                      <Badge>Fallback</Badge> {fallback}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          </>
        ) : null}
        <h4 className="mt-ol-m mb-ol-s text-ol-m">Errors ({diagnostics.errors.length})</h4>
        {diagnostics.errors.length === 0 ? (
          <p>No errors</p>
        ) : (
          <ul data-testid="diagnostics-errors" className="flex flex-col gap-ol-xs my-ol-s pl-ol-m">
            {diagnostics.errors.map((error) => (
              <li key={error}>{error}</li>
            ))}
          </ul>
        )}
        <details data-testid="diagnostics-developer" className="mt-ol-s">
          <summary className="cursor-pointer font-semibold text-ol-s">Developer details</summary>
          <pre className="whitespace-pre-wrap break-words text-ol-s">
            {JSON.stringify(diagnostics, null, 2)}
          </pre>
        </details>
      </Drawer>
    </GlassPanel>
  );
}
