import { useState } from 'react';
import type { Diagnostics } from '../api/types';

export interface DiagnosticsDrawerProps {
  diagnostics: Diagnostics;
}

/** Collapsible dev panel rendering the response diagnostics block. */
export function DiagnosticsDrawer({ diagnostics }: DiagnosticsDrawerProps) {
  const [open, setOpen] = useState(false);
  const latencies = Object.entries(diagnostics.provider_latencies_ms);

  return (
    <section aria-label="Diagnostics">
      <button
        type="button"
        aria-expanded={open}
        aria-controls="diagnostics-panel"
        onClick={() => setOpen((v) => !v)}
      >
        {open ? 'Hide diagnostics' : 'Show diagnostics'}
      </button>
      {open ? (
        <div id="diagnostics-panel" data-testid="diagnostics-panel">
          <p data-testid="diagnostics-fallback">
            Fallback used: {diagnostics.fallback_used ? 'yes' : 'no'}
          </p>
          <h4>Provider latencies</h4>
          {latencies.length === 0 ? (
            <p>No provider latencies reported</p>
          ) : (
            <dl data-testid="diagnostics-latencies">
              {latencies.map(([provider, ms]) => (
                <div key={provider}>
                  <dt>{provider}</dt>
                  <dd>{ms} ms</dd>
                </div>
              ))}
            </dl>
          )}
          <h4>Plugin executions ({diagnostics.plugin_executions.length})</h4>
          {diagnostics.plugin_executions.length === 0 ? (
            <p>No plugin executions</p>
          ) : (
            <ul data-testid="diagnostics-plugins">
              {diagnostics.plugin_executions.map((execution, index) => (
                <li key={index}>
                  <pre>{JSON.stringify(execution)}</pre>
                </li>
              ))}
            </ul>
          )}
          <h4>Errors ({diagnostics.errors.length})</h4>
          {diagnostics.errors.length === 0 ? (
            <p>No errors</p>
          ) : (
            <ul data-testid="diagnostics-errors">
              {diagnostics.errors.map((error) => (
                <li key={error}>{error}</li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </section>
  );
}
