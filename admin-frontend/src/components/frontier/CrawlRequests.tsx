import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { decideCrawlRequest, listCrawlRequests } from '../../api/client.ts';
import type { CrawlRequest } from '../../api/types.ts';
import { Button } from '../ui/Button.tsx';
import { ConfirmDialog } from '../ui/ConfirmDialog.tsx';
import { DataTable } from '../ui/DataTable.tsx';
import { Toasts, useToasts } from '../ui/Toast.tsx';

export type CrawlRequestStatus = 'pending' | 'approved' | 'declined';

const STATUSES: CrawlRequestStatus[] = ['pending', 'approved', 'declined'];
const PAGE_SIZE = 20;

export interface CrawlRequestsProps {
  adminToken: string;
  /** Called after an approve/decline so the shell can refresh the queue. */
  onChanged: () => void;
}

interface PendingDecision {
  requests: CrawlRequest[];
  decision: 'approve' | 'decline';
}

/**
 * User-requested crawl URLs awaiting review. Status tabs use the
 * backend filter the drawer previously hardcoded to `pending`;
 * search/pagination/selection run client-side over the fetched list
 * (the list endpoint takes only `status`). Every decision confirms
 * first — previously approve/decline toasted with no confirmation.
 */
export function CrawlRequests({ adminToken, onChanged }: CrawlRequestsProps) {
  const [status, setStatus] = useState<CrawlRequestStatus>('pending');
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState('url');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc');
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const [pending, setPending] = useState<PendingDecision | null>(null);
  const { items: toasts, push: pushToast, dismiss: dismissToast } = useToasts();

  const requestsQuery = useQuery({
    queryKey: ['frontier', 'crawl-requests', adminToken, status],
    queryFn: () => listCrawlRequests(adminToken, status),
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const requests: CrawlRequest[] = requestsQuery.data?.requests ?? [];

  function changeStatus(next: CrawlRequestStatus): void {
    setStatus(next);
    setPage(0);
    setSelected([]);
    setSearch('');
  }

  const visible = useMemo(() => {
    const query = search.trim().toLowerCase();
    const filtered =
      query === '' ? requests : requests.filter((entry) => entry.url.toLowerCase().includes(query));
    return [...filtered].sort((a, b) =>
      sortDir === 'asc' ? a.url.localeCompare(b.url) : b.url.localeCompare(a.url),
    );
  }, [requests, search, sortDir]);

  const pageCount = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const total = requestsQuery.data?.total ?? requests.length;

  async function applyDecision(decision: PendingDecision): Promise<void> {
    setPending(null);
    let ok = 0;
    for (const entry of decision.requests) {
      if (entry.id === null) continue;
      try {
        const decided = await decideCrawlRequest(entry.id, decision.decision, adminToken);
        ok += 1;
        pushToast(
          decision.decision === 'approve'
            ? `Approved — ${decided.url} queued for crawling.`
            : `Declined — ${decided.url} will not be crawled.`,
          'success',
        );
      } catch {
        pushToast(decision.decision === 'approve' ? 'Approve failed.' : 'Decline failed.', 'error');
      }
    }
    if (ok > 0) {
      setSelected([]);
      await requestsQuery.refetch();
      onChanged();
    }
  }

  return (
    <section aria-label="Crawl requests">
      <h2 className="ol-dash-title">Crawl requests ({total})</h2>
      <div className="flex flex-wrap gap-ol-xs my-ol-s" role="group" aria-label="Request status">
        {STATUSES.map((entry) => (
          <Button
            key={entry}
            variant={status === entry ? 'primary' : 'secondary'}
            size="sm"
            data-testid={`crawl-requests-tab-${entry}`}
            aria-pressed={status === entry}
            onClick={() => changeStatus(entry)}
          >
            {entry[0].toUpperCase() + entry.slice(1)}
          </Button>
        ))}
      </div>
      {requestsQuery.isPending ? (
        <p className="my-ol-s text-ol-s">Loading crawl requests…</p>
      ) : requestsQuery.isError || requestsQuery.data == null ? (
        <div role="alert" className="my-ol-s text-ol-s">
          <p>Could not load crawl requests.</p>
          <Button variant="secondary" size="sm" onClick={() => void requestsQuery.refetch()}>
            Retry
          </Button>
        </div>
      ) : visible.length === 0 && search.trim() === '' ? (
        <p className="my-ol-s text-ol-s">No URLs awaiting review</p>
      ) : (
        <>
          <DataTable<CrawlRequest>
            rows={visible}
            rowKey={(entry) => String(entry.id ?? entry.url)}
            ariaLabel="Crawl requests"
            testIdPrefix="frontier-crawl-requests"
            search={{
              value: search,
              onChange: (value) => {
                setSearch(value);
                setPage(0);
              },
              placeholder: 'Search request URLs…',
              ariaLabel: 'Search crawl requests',
              keys: [(entry) => entry.url],
            }}
            sort={{
              value: sortKey,
              direction: sortDir,
              onChange: (key, dir) => {
                setSortKey(key);
                setSortDir(dir);
              },
            }}
            pagination={{ page: safePage, pageSize: PAGE_SIZE, onPage: setPage }}
            selection={{
              selected,
              onChange: setSelected,
              label: (count) => `${count} request(s) selected`,
            }}
            columns={[
              {
                key: 'url',
                header: 'URL',
                sortable: true,
                sortValue: (entry) => entry.url,
                render: (entry) => <span className="text-ol-s break-all">{entry.url}</span>,
              },
              {
                key: 'actions',
                header: 'Decision',
                render: (entry) => (
                  <span className="inline-flex flex-wrap gap-ol-xs">
                    <Button
                      variant="secondary"
                      size="sm"
                      data-testid={`crawl-request-approve-${entry.id}`}
                      onClick={() => setPending({ requests: [entry], decision: 'approve' })}
                      disabled={entry.id === null}
                    >
                      Approve
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      data-testid={`crawl-request-decline-${entry.id}`}
                      onClick={() => setPending({ requests: [entry], decision: 'decline' })}
                      disabled={entry.id === null}
                    >
                      Decline
                    </Button>
                  </span>
                ),
              },
            ]}
          />
          {selected.length > 0 ? (
            <div className="mt-ol-s flex flex-wrap gap-ol-s">
              <Button
                variant="secondary"
                size="sm"
                data-testid="crawl-requests-bulk-approve"
                onClick={() =>
                  setPending({
                    requests: visible.filter((entry) =>
                      selected.includes(String(entry.id ?? entry.url)),
                    ),
                    decision: 'approve',
                  })
                }
              >
                Approve selected ({selected.length})
              </Button>
              <Button
                variant="destructive"
                size="sm"
                data-testid="crawl-requests-bulk-decline"
                onClick={() =>
                  setPending({
                    requests: visible.filter((entry) =>
                      selected.includes(String(entry.id ?? entry.url)),
                    ),
                    decision: 'decline',
                  })
                }
              >
                Decline selected ({selected.length})
              </Button>
            </div>
          ) : null}
        </>
      )}
      <ConfirmDialog
        open={pending !== null}
        title={
          pending?.decision === 'approve'
            ? `Approve ${pending?.requests.length ?? 0} request(s)?`
            : `Decline ${pending?.requests.length ?? 0} request(s)?`
        }
        body={
          pending?.decision === 'approve'
            ? 'Approved URLs join the crawl queue immediately.'
            : 'Declined URLs will not be crawled. This parks the request.'
        }
        confirmLabel={pending?.decision === 'approve' ? 'Approve' : 'Decline'}
        tone={pending?.decision === 'approve' ? 'default' : 'destructive'}
        onConfirm={() => {
          if (pending) void applyDecision(pending);
        }}
        onCancel={() => setPending(null)}
      />
      <Toasts items={toasts} onDismiss={dismissToast} />
    </section>
  );
}
