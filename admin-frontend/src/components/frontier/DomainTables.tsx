import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  fetchFailedDomains,
  fetchFailedDomainUrls,
  fetchFailedUrls,
  fetchQueuedDomains,
  fetchQueuedDomainUrls,
} from '../../api/client.ts';
import type {
  FrontierFailedDomain,
  FrontierFailedUrl,
  FrontierQueuedDomain,
} from '../../api/types.ts';
import type { FrontierDomainOp } from '../../api/client.ts';
import { Badge } from '../ui/Badge.tsx';
import { Button } from '../ui/Button.tsx';
import { ConfirmDialog } from '../ui/ConfirmDialog.tsx';
import { DataTable } from '../ui/DataTable.tsx';
import { Skeleton } from '../ui/Skeleton.tsx';
import { Toasts, useToasts } from '../ui/Toast.tsx';

const PAGE_SIZE = 20;

export interface DomainTablesProps {
  /** Single-domain action (requeue/purge/skip); bulk passes many. */
  onDomainAction: (op: FrontierDomainOp, domains: string[]) => Promise<void>;
  onBlockDomain: (domain: string) => Promise<void>;
  onAutoTriage: () => Promise<void>;
  onQueuedPriority: (domain: string, delta: number) => Promise<void>;
  /** Called after any mutation so the shell can refresh the snapshot. */
  onMutated: () => void;
}

interface PendingBulk {
  kind: 'op' | 'block' | 'priority' | 'triage';
  op?: FrontierDomainOp;
  domains: string[];
  delta?: number;
  summary: string;
}

/**
 * Failed URLs, failed-domain and queued-domain rollups with drill-downs,
 * extracted from the frontier drawer onto `DataTable`: client search and
 * sort over the fetched page, server offset paging preserved, selection
 * checkboxes feeding bulk Requeue/Flush/Skip/Block and bulk priority.
 * Per-row buttons keep the single-domain handlers as their degenerate
 * case; destructive actions confirm first.
 */
export function DomainTables({
  onDomainAction,
  onBlockDomain,
  onAutoTriage,
  onQueuedPriority,
  onMutated,
}: DomainTablesProps) {
  const { items: toasts, push: pushToast, dismiss: dismissToast } = useToasts();
  const [pending, setPending] = useState<PendingBulk | null>(null);

  // Failed URLs (flat list) + failed-domain rollup + drill-down.
  const [failedPage, setFailedPage] = useState(0);
  const [failedSearch, setFailedSearch] = useState('');
  const [failedSort, setFailedSort] = useState<{ key: string; dir: 'asc' | 'desc' }>({
    key: 'attempts',
    dir: 'desc',
  });
  const [failedDomainPage, setFailedDomainPage] = useState(0);
  const [failedDomainSearch, setFailedDomainSearch] = useState('');
  const [failedSelected, setFailedSelected] = useState<string[]>([]);
  const [expandedDomain, setExpandedDomain] = useState<string | null>(null);
  const [domainUrlPage, setDomainUrlPage] = useState(0);

  // Queued-domain rollup + drill-down.
  const [queuedDomainPage, setQueuedDomainPage] = useState(0);
  const [queuedSearch, setQueuedSearch] = useState('');
  const [queuedSelected, setQueuedSelected] = useState<string[]>([]);
  const [expandedQueuedDomain, setExpandedQueuedDomain] = useState<string | null>(null);
  const [queuedDomainUrlPage, setQueuedDomainUrlPage] = useState(0);

  const failedQuery = useQuery({
    queryKey: ['frontier', 'failed', failedPage],
    queryFn: () => fetchFailedUrls(PAGE_SIZE, failedPage * PAGE_SIZE),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const failedDomainsQuery = useQuery({
    queryKey: ['frontier', 'failed-domains', failedDomainPage],
    queryFn: () => fetchFailedDomains(PAGE_SIZE, failedDomainPage * PAGE_SIZE),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const domainUrlsQuery = useQuery({
    queryKey: ['frontier', 'failed-domain-urls', expandedDomain, domainUrlPage],
    queryFn: () =>
      expandedDomain == null
        ? Promise.resolve(null)
        : fetchFailedDomainUrls(expandedDomain, PAGE_SIZE, domainUrlPage * PAGE_SIZE),
    enabled: expandedDomain != null,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const queuedDomainsQuery = useQuery({
    queryKey: ['frontier', 'queued-domains', queuedDomainPage],
    queryFn: () => fetchQueuedDomains(PAGE_SIZE, queuedDomainPage * PAGE_SIZE),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const queuedDomainUrlsQuery = useQuery({
    queryKey: ['frontier', 'queued-domain-urls', expandedQueuedDomain, queuedDomainUrlPage],
    queryFn: () =>
      expandedQueuedDomain == null
        ? Promise.resolve(null)
        : fetchQueuedDomainUrls(expandedQueuedDomain, PAGE_SIZE, queuedDomainUrlPage * PAGE_SIZE),
    enabled: expandedQueuedDomain != null,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });

  const failedTotal = failedQuery.data?.total ?? 0;
  const failedDomainTotal = failedDomainsQuery.data?.total ?? 0;
  const queuedDomainTotal = queuedDomainsQuery.data?.total ?? 0;

  function toggleDomainUrls(domain: string): void {
    setExpandedDomain((current) => (current === domain ? null : domain));
    setDomainUrlPage(0);
  }

  function toggleQueuedDomainUrls(domain: string): void {
    setExpandedQueuedDomain((current) => (current === domain ? null : domain));
    setQueuedDomainUrlPage(0);
  }

  async function refreshLists(): Promise<void> {
    await Promise.all([
      failedQuery.refetch(),
      failedDomainsQuery.refetch(),
      queuedDomainsQuery.refetch(),
    ]);
    onMutated();
  }

  async function runSingle(op: FrontierDomainOp, domain: string): Promise<void> {
    try {
      await onDomainAction(op, [domain]);
      await refreshLists();
    } catch {
      pushToast(
        `${op === 'requeue' ? 'Requeue' : op === 'purge' ? 'Flush' : 'Skip'} failed.`,
        'error',
      );
    }
  }

  async function runPriority(domain: string, delta: number): Promise<void> {
    try {
      await onQueuedPriority(domain, delta);
      await refreshLists();
    } catch {
      pushToast('Priority change failed.', 'error');
    }
  }

  async function runTriage(): Promise<void> {
    setPending(null);
    try {
      await onAutoTriage();
      await refreshLists();
    } catch {
      pushToast('Auto-triage failed.', 'error');
    }
  }

  async function applyPending(): Promise<void> {
    if (!pending) return;
    const current = pending;
    setPending(null);
    try {
      if (current.kind === 'triage') {
        await runTriage();
        return;
      }
      if (current.kind === 'op' && current.op) {
        await onDomainAction(current.op, current.domains);
      } else if (current.kind === 'block') {
        try {
          for (const domain of current.domains) {
            await onBlockDomain(domain);
          }
        } catch {
          pushToast('Block failed.', 'error');
          return;
        }
        pushToast(`Blocked — ${current.summary} added to deny_domains.`, 'success');
      } else if (current.kind === 'priority' && current.delta !== undefined) {
        try {
          for (const domain of current.domains) {
            await onQueuedPriority(domain, current.delta);
          }
        } catch {
          pushToast('Priority change failed.', 'error');
          return;
        }
      } else {
        return;
      }
      setFailedSelected([]);
      setQueuedSelected([]);
      await refreshLists();
    } catch {
      pushToast('Domain action failed.', 'error');
    }
  }

  const failedRows: FrontierFailedUrl[] = failedQuery.data?.urls ?? [];
  const failedDomainRows: FrontierFailedDomain[] = failedDomainsQuery.data?.domains ?? [];
  const queuedDomainRows: FrontierQueuedDomain[] = queuedDomainsQuery.data?.domains ?? [];

  return (
    <>
      <section aria-label="Failed URLs">
        <h2 className="ol-dash-title">Failed URLs ({failedTotal})</h2>
        {failedQuery.isPending ? (
          <div aria-live="polite">
            <Skeleton bars={3} />
          </div>
        ) : failedQuery.isError || failedQuery.data == null ? (
          <div role="alert" className="my-ol-s text-ol-s">
            <p>Could not load failed URLs.</p>
            <Button variant="secondary" size="sm" onClick={() => void failedQuery.refetch()}>
              Retry
            </Button>
          </div>
        ) : (
          <DataTable<FrontierFailedUrl>
            rows={failedRows}
            rowKey={(entry) => entry.url}
            ariaLabel="Failed URLs"
            testIdPrefix="frontier-failed"
            search={{
              value: failedSearch,
              onChange: (value) => setFailedSearch(value),
              placeholder: 'Search URLs or reasons…',
              ariaLabel: 'Search failed URLs',
              keys: [(entry) => entry.url, (entry) => entry.reason ?? ''],
            }}
            sort={{
              value: failedSort.key,
              direction: failedSort.dir,
              onChange: (key, dir) => setFailedSort({ key, dir }),
            }}
            pagination={{
              page: failedPage,
              pageSize: PAGE_SIZE,
              total: failedTotal,
              onPage: setFailedPage,
            }}
            empty={{ title: 'No failed URLs', testId: 'frontier-failed-empty' }}
            columns={[
              {
                key: 'attempts',
                header: 'Tries',
                sortable: true,
                sortValue: (entry) => entry.attempts ?? 0,
                render: (entry) => <Badge>{entry.attempts ?? 0} tries</Badge>,
              },
              {
                key: 'url',
                header: 'URL',
                sortable: true,
                sortValue: (entry) => entry.url,
                render: (entry) => (
                  <span className="break-all">
                    {entry.url}
                    {entry.reason ? <span className="text-ol-s"> · {entry.reason}</span> : null}
                  </span>
                ),
              },
            ]}
          />
        )}
      </section>

      <section aria-label="Needs attention" data-testid="frontier-needs-attention">
        <h2 className="ol-dash-title">Needs attention</h2>
        <h3 className="mt-ol-m mb-ol-s text-ol-m">Failed domains ({failedDomainTotal})</h3>
        {failedDomainTotal > 0 ? (
          <Button
            variant="secondary"
            size="sm"
            data-testid="frontier-auto-triage"
            onClick={() => setPending({ kind: 'triage', domains: [], summary: 'auto-triage' })}
          >
            Auto-triage
          </Button>
        ) : null}
        {failedDomainsQuery.isPending ? (
          <div aria-live="polite">
            <Skeleton bars={3} />
          </div>
        ) : failedDomainsQuery.isError || failedDomainsQuery.data == null ? (
          <div role="alert" className="my-ol-s text-ol-s">
            <p>Could not load failed domains.</p>
            <Button variant="secondary" size="sm" onClick={() => void failedDomainsQuery.refetch()}>
              Retry
            </Button>
          </div>
        ) : (
          <>
            <DataTable<FrontierFailedDomain>
              rows={failedDomainRows}
              rowKey={(entry) => entry.domain}
              ariaLabel="Failed domains"
              testIdPrefix="frontier-failed-domains"
              search={{
                value: failedDomainSearch,
                onChange: (value) => setFailedDomainSearch(value),
                placeholder: 'Search domains…',
                ariaLabel: 'Search failed domains',
                keys: ['domain', (entry) => entry.top_reason],
              }}
              pagination={{
                page: failedDomainPage,
                pageSize: PAGE_SIZE,
                total: failedDomainTotal,
                onPage: setFailedDomainPage,
              }}
              selection={{
                selected: failedSelected,
                onChange: setFailedSelected,
                label: (count) => `${count} domain(s) selected`,
              }}
              empty={{ title: 'No failed domains', testId: 'frontier-failed-domains-empty' }}
              columns={[
                {
                  key: 'domain',
                  header: 'Domain',
                  render: (entry) => (
                    <span className="break-all">
                      <Badge>{entry.failed}</Badge>{' '}
                      <span className="ol-domain-name">{entry.domain}</span>{' '}
                      <span className="text-ol-s">{entry.top_reason}</span>
                    </span>
                  ),
                },
                {
                  key: 'actions',
                  header: 'Actions',
                  render: (entry) => (
                    <span className="inline-flex flex-wrap gap-ol-xs">
                      <Button
                        variant="secondary"
                        size="sm"
                        data-testid={`frontier-domain-toggle-${entry.domain}`}
                        onClick={() => toggleDomainUrls(entry.domain)}
                      >
                        {expandedDomain === entry.domain ? 'Hide URLs' : 'Show URLs'}
                      </Button>
                      <Button
                        variant="secondary"
                        size="sm"
                        data-testid={`frontier-domain-requeue-${entry.domain}`}
                        onClick={() => void runSingle('requeue', entry.domain)}
                      >
                        Requeue
                      </Button>
                      <Button
                        variant="destructive"
                        size="sm"
                        data-testid={`frontier-domain-flush-${entry.domain}`}
                        onClick={() =>
                          setPending({
                            kind: 'op',
                            op: 'purge',
                            domains: [entry.domain],
                            summary: entry.domain,
                          })
                        }
                      >
                        Flush
                      </Button>
                      <Button
                        variant="secondary"
                        size="sm"
                        data-testid={`frontier-domain-skip-${entry.domain}`}
                        onClick={() => void runSingle('skip', entry.domain)}
                      >
                        Skip
                      </Button>
                      <Button
                        variant="destructive"
                        size="sm"
                        data-testid={`frontier-domain-block-${entry.domain}`}
                        onClick={() =>
                          setPending({
                            kind: 'block',
                            domains: [entry.domain],
                            summary: entry.domain,
                          })
                        }
                      >
                        Block
                      </Button>
                    </span>
                  ),
                },
              ]}
            />
            {failedSelected.length > 0 ? (
              <div
                className="mt-ol-s flex flex-wrap gap-ol-s"
                role="group"
                aria-label="Bulk domain actions"
              >
                <Button
                  variant="secondary"
                  size="sm"
                  data-testid="frontier-failed-domains-bulk-requeue"
                  onClick={() =>
                    setPending({
                      kind: 'op',
                      op: 'requeue',
                      domains: [...failedSelected],
                      summary: `${failedSelected.length} domain(s)`,
                    })
                  }
                >
                  Requeue selected ({failedSelected.length})
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  data-testid="frontier-failed-domains-bulk-flush"
                  onClick={() =>
                    setPending({
                      kind: 'op',
                      op: 'purge',
                      domains: [...failedSelected],
                      summary: `${failedSelected.length} domain(s)`,
                    })
                  }
                >
                  Flush selected ({failedSelected.length})
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  data-testid="frontier-failed-domains-bulk-skip"
                  onClick={() =>
                    setPending({
                      kind: 'op',
                      op: 'skip',
                      domains: [...failedSelected],
                      summary: `${failedSelected.length} domain(s)`,
                    })
                  }
                >
                  Skip selected ({failedSelected.length})
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  data-testid="frontier-failed-domains-bulk-block"
                  onClick={() =>
                    setPending({
                      kind: 'block',
                      domains: [...failedSelected],
                      summary: `${failedSelected.length} domain(s)`,
                    })
                  }
                >
                  Block selected ({failedSelected.length})
                </Button>
              </div>
            ) : null}
            {expandedDomain != null ? (
              <div className="my-ol-s">
                {domainUrlsQuery.isPending ? (
                  <p className="text-ol-s">Loading URLs…</p>
                ) : domainUrlsQuery.data == null ? (
                  <p className="text-ol-s">Could not load URLs.</p>
                ) : (
                  <DataTable<FrontierFailedUrl>
                    rows={domainUrlsQuery.data.urls}
                    rowKey={(entry) => entry.url}
                    ariaLabel={`Failed URLs for ${expandedDomain}`}
                    testIdPrefix={`frontier-domain-urls-${expandedDomain}`}
                    pagination={{
                      page: domainUrlPage,
                      pageSize: PAGE_SIZE,
                      total: domainUrlsQuery.data.total,
                      onPage: setDomainUrlPage,
                    }}
                    empty={{
                      title: 'No failed URLs for this domain',
                      testId: `frontier-domain-urls-empty-${expandedDomain}`,
                    }}
                    columns={[
                      {
                        key: 'url',
                        header: 'URL',
                        render: (entry) => (
                          <span className="break-all">
                            <Badge>{entry.attempts ?? 0}</Badge> {entry.url}{' '}
                            <span className="text-ol-s">· {entry.reason}</span>
                          </span>
                        ),
                      },
                    ]}
                  />
                )}
              </div>
            ) : null}
          </>
        )}
      </section>

      <section aria-label="Queued by domain">
        <h2 className="ol-dash-title">Queued by domain ({queuedDomainTotal})</h2>
        {queuedDomainsQuery.isPending ? (
          <div aria-live="polite">
            <Skeleton bars={3} />
          </div>
        ) : queuedDomainsQuery.isError || queuedDomainsQuery.data == null ? (
          <div role="alert" className="my-ol-s text-ol-s">
            <p>Could not load queued domains.</p>
            <Button variant="secondary" size="sm" onClick={() => void queuedDomainsQuery.refetch()}>
              Retry
            </Button>
          </div>
        ) : (
          <>
            <DataTable<FrontierQueuedDomain>
              rows={queuedDomainRows}
              rowKey={(entry) => entry.domain}
              ariaLabel="Queued domains"
              testIdPrefix="frontier-queued-domains"
              search={{
                value: queuedSearch,
                onChange: (value) => setQueuedSearch(value),
                placeholder: 'Search domains…',
                ariaLabel: 'Search queued domains',
                keys: ['domain'],
              }}
              pagination={{
                page: queuedDomainPage,
                pageSize: PAGE_SIZE,
                total: queuedDomainTotal,
                onPage: setQueuedDomainPage,
              }}
              selection={{
                selected: queuedSelected,
                onChange: setQueuedSelected,
                label: (count) => `${count} domain(s) selected`,
              }}
              empty={{ title: 'No queued domains', testId: 'frontier-queued-domains-empty' }}
              columns={[
                {
                  key: 'domain',
                  header: 'Domain',
                  render: (entry) => (
                    <span className="break-all">
                      <Badge>{entry.queued}</Badge> <Badge>p{entry.top_priority}</Badge>{' '}
                      <span className="ol-domain-name">{entry.domain}</span>
                    </span>
                  ),
                },
                {
                  key: 'actions',
                  header: 'Actions',
                  render: (entry) => (
                    <span className="inline-flex flex-wrap gap-ol-xs">
                      <Button
                        variant="secondary"
                        size="sm"
                        data-testid={`frontier-queued-toggle-${entry.domain}`}
                        onClick={() => toggleQueuedDomainUrls(entry.domain)}
                      >
                        {expandedQueuedDomain === entry.domain ? 'Hide URLs' : 'Show URLs'}
                      </Button>
                      <Button
                        variant="secondary"
                        size="sm"
                        data-testid={`frontier-queued-prio-up-${entry.domain}`}
                        onClick={() => void runPriority(entry.domain, 1)}
                      >
                        +1 priority
                      </Button>
                      <Button
                        variant="secondary"
                        size="sm"
                        data-testid={`frontier-queued-prio-down-${entry.domain}`}
                        onClick={() => void runPriority(entry.domain, -1)}
                      >
                        −1 priority
                      </Button>
                      <Button
                        variant="secondary"
                        size="sm"
                        data-testid={`frontier-queued-skip-${entry.domain}`}
                        onClick={() => void runSingle('skip', entry.domain)}
                      >
                        Skip
                      </Button>
                      <Button
                        variant="destructive"
                        size="sm"
                        data-testid={`frontier-queued-block-${entry.domain}`}
                        onClick={() =>
                          setPending({
                            kind: 'block',
                            domains: [entry.domain],
                            summary: entry.domain,
                          })
                        }
                      >
                        Block
                      </Button>
                    </span>
                  ),
                },
              ]}
            />
            {queuedSelected.length > 0 ? (
              <div
                className="mt-ol-s flex flex-wrap gap-ol-s"
                role="group"
                aria-label="Bulk queued actions"
              >
                <Button
                  variant="secondary"
                  size="sm"
                  data-testid="frontier-queued-domains-bulk-prio-up"
                  onClick={() =>
                    setPending({
                      kind: 'priority',
                      domains: [...queuedSelected],
                      delta: 1,
                      summary: `${queuedSelected.length} domain(s)`,
                    })
                  }
                >
                  +1 priority selected ({queuedSelected.length})
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  data-testid="frontier-queued-domains-bulk-prio-down"
                  onClick={() =>
                    setPending({
                      kind: 'priority',
                      domains: [...queuedSelected],
                      delta: -1,
                      summary: `${queuedSelected.length} domain(s)`,
                    })
                  }
                >
                  −1 priority selected ({queuedSelected.length})
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  data-testid="frontier-queued-domains-bulk-skip"
                  onClick={() =>
                    setPending({
                      kind: 'op',
                      op: 'skip',
                      domains: [...queuedSelected],
                      summary: `${queuedSelected.length} domain(s)`,
                    })
                  }
                >
                  Skip selected ({queuedSelected.length})
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  data-testid="frontier-queued-domains-bulk-block"
                  onClick={() =>
                    setPending({
                      kind: 'block',
                      domains: [...queuedSelected],
                      summary: `${queuedSelected.length} domain(s)`,
                    })
                  }
                >
                  Block selected ({queuedSelected.length})
                </Button>
              </div>
            ) : null}
            {expandedQueuedDomain != null ? (
              <div className="my-ol-s">
                {queuedDomainUrlsQuery.isPending ? (
                  <p className="text-ol-s">Loading URLs…</p>
                ) : queuedDomainUrlsQuery.data == null ? (
                  <p className="text-ol-s">Could not load URLs.</p>
                ) : (
                  <DataTable<FrontierFailedUrl>
                    rows={queuedDomainUrlsQuery.data.urls}
                    rowKey={(entry) => entry.url}
                    ariaLabel={`Queued URLs for ${expandedQueuedDomain}`}
                    testIdPrefix={`frontier-queued-urls-${expandedQueuedDomain}`}
                    pagination={{
                      page: queuedDomainUrlPage,
                      pageSize: PAGE_SIZE,
                      total: queuedDomainUrlsQuery.data.total,
                      onPage: setQueuedDomainUrlPage,
                    }}
                    empty={{ title: 'No queued URLs for this domain' }}
                    columns={[
                      {
                        key: 'url',
                        header: 'URL',
                        render: (entry) => (
                          <span className="break-all">
                            {(entry.attempts ?? 0) > 0 ? <Badge>{entry.attempts}</Badge> : null}{' '}
                            {entry.url}
                            {entry.reason ? (
                              <span className="text-ol-s"> · {entry.reason}</span>
                            ) : null}
                          </span>
                        ),
                      },
                    ]}
                  />
                )}
              </div>
            ) : null}
          </>
        )}
      </section>

      <ConfirmDialog
        open={pending !== null}
        title={
          pending?.kind === 'triage'
            ? 'Auto-triage every failed domain?'
            : pending?.kind === 'priority'
              ? `Change priority for ${pending?.domains.length ?? 0} domain(s)?`
              : pending?.kind === 'block'
                ? `Block ${pending?.summary}?`
                : pending?.op === 'purge'
                  ? `Permanently delete failed history for ${pending?.summary}?`
                  : pending?.op === 'requeue'
                    ? `Requeue ${pending?.summary}?`
                    : `Skip ${pending?.summary}?`
        }
        body={
          pending?.kind === 'triage'
            ? 'Robots-disallowed, content-type, 404/401 are skipped and flushed; 429 and timeouts are requeued with backoff; thin content is also blocked. Unknown reasons are left alone.'
            : pending?.kind === 'block'
              ? 'Its queued rows keep their policy gate immediately; workers pick the config up on refresh.'
              : pending?.op === 'purge'
                ? 'Queued, active, and done rows are kept.'
                : pending?.kind === 'priority'
                  ? 'Higher dequeues first; lower sinks.'
                  : 'This acts on every selected domain at once.'
        }
        confirmLabel={
          pending?.kind === 'triage'
            ? 'Auto-triage'
            : pending?.kind === 'block'
              ? 'Block'
              : pending?.op === 'purge'
                ? 'Flush'
                : pending?.op === 'requeue'
                  ? 'Requeue'
                  : pending?.kind === 'priority'
                    ? 'Change priority'
                    : 'Skip'
        }
        tone={pending?.kind === 'block' || pending?.op === 'purge' ? 'destructive' : 'default'}
        onConfirm={() => void applyPending()}
        onCancel={() => setPending(null)}
      />
      <Toasts items={toasts} onDismiss={dismissToast} />
    </>
  );
}
