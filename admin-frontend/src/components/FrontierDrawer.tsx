import { useEffect, useState } from 'react';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import {
  autoTriageFailedDomains,
  blockDomains,
  bumpDomainPriority,
  clearFrontier,
  decideCrawlRequest,
  domainAction,
  fetchFailedDomains,
  fetchFailedDomainUrls,
  fetchFailedUrls,
  fetchQueuedDomainUrls,
  fetchQueuedDomains,
  frontierLiveUrl,
  listCrawlRequests,
  pauseFrontier,
  resumeFrontier,
  saveFrontierSettings,
} from '../api/client.ts';
import type { FrontierDomainOp } from '../api/client.ts';
import {
  frontierGetSettingsApiV1FrontierSettingsGet,
  frontierListWorkersApiV1FrontierWorkersGet,
  frontierStatusApiV1FrontierStatusGet,
} from '../api/generated/openlens.ts';
import {
  isFrontierStatus,
  readFrontierDiscovery,
  readFrontierNewUrls,
  readFrontierQueue,
  readFrontierSettings,
  readFrontierWorkers,
} from '../api/guard.ts';
import type {
  CrawlRequest,
  FrontierFailedDomainList,
  FrontierFailedList,
  FrontierQueuedDomainList,
  FrontierSettings,
  FrontierStatus,
  FrontierWorker,
} from '../api/types.ts';
import { Badge } from './ui/Badge.tsx';
import { Button } from './ui/Button.tsx';
/** Format a per-minute rate; em dash when the worker has no sample yet. */
function formatRate(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${value >= 10 ? Math.round(value) : value.toFixed(1)}/min`;
}

/** Sum helper that skips missing rates. */
function sumRates(
  workers: FrontierWorker[],
  key: 'pages_per_min' | 'tokens_per_min',
): number | null {
  let total = 0;
  let seen = false;
  for (const worker of workers) {
    const value = worker[key];
    if (typeof value === 'number' && !Number.isNaN(value)) {
      total += value;
      seen = true;
    }
  }
  return seen ? total : null;
}
import { Card } from './ui/Card.tsx';
import { Drawer } from './ui/Drawer.tsx';
import { GlassPanel } from './ui/GlassPanel.tsx';
import { Toasts, useToasts } from './ui/Toast.tsx';

/** Failed-URL page size for the paginated admin list. */
const FAILED_PAGE_SIZE = 20;

/** Local defaults used before the backend settings load. */
const DEFAULT_SETTINGS: FrontierSettings = {
  max_pages: 100,
  max_queued_urls: 1000,
  crawl_delay_s: 0.5,
  default_max_depth: 3,
  respect_robots: true,
  user_agent: 'OpenLensBot/1.0 (+https://github.com/rajanbajaj/openlens)',
  skip_domains: [],
  allow_domains: [],
  deny_domains: [],
  discovery_feeds: [],
  refresh_after_days: 30.0,
  discovery_interval_s: 21600.0,
};

/**
 * Status + settings + workers snapshot loaded through the generated
 * client. Workers come from the dedicated endpoint (the source of
 * truth for this replica's registry); when it is unreachable the
 * workers arriving inside the status payload are used as a fallback.
 */
async function loadFrontierSnapshot(): Promise<{
  status: FrontierStatus | null;
  settings: FrontierSettings;
  workers: FrontierWorker[];
}> {
  let status: FrontierStatus | null = null;
  try {
    const res = await frontierStatusApiV1FrontierStatusGet();
    if (res.status === 200 && isFrontierStatus(res.data)) status = res.data;
  } catch {
    status = null;
  }
  let workers: FrontierWorker[] = status ? readFrontierWorkers(status) : [];
  try {
    const res = await frontierListWorkersApiV1FrontierWorkersGet();
    if (res.status === 200) {
      const listed = readFrontierWorkers({ workers: res.data.workers });
      if (listed.length > 0 || (res.data.worker_count ?? 0) === 0) workers = listed;
    }
  } catch {
    /* keep the status-payload fallback when the endpoint fails */
  }
  let settings = DEFAULT_SETTINGS;
  try {
    const res = await frontierGetSettingsApiV1FrontierSettingsGet();
    const parsed = readFrontierSettings(res.status === 200 ? res.data : null);
    if (parsed) settings = parsed;
  } catch {
    /* keep defaults when settings fail independently */
  }
  return { status, settings, workers };
}

/** Props for the frontier dashboard: the admin token comes from the app shell. */
export interface FrontierDrawerProps {
  adminToken: string;
}

/**
 * Frontier dashboard: crawl-queue view, pause/resume controls,
 * checkpoint status, new-URL list, and settings form. Moved here from
 * the user frontend so queue/worker mutations stay admin-only behind
 * the backend `X-Admin-Token` gate. Loads its data lazily on first
 * open via TanStack Query (generated typed client); every fetch
 * failure degrades to an empty view with an error toast instead of
 * crashing the page. All submissions confirm via toaster popups.
 */
function FrontierDrawerView({ adminToken }: FrontierDrawerProps) {
  // Open by default: in the admin app this is a page, not a rail drawer.
  const [open, setOpen] = useState(true);
  const [status, setStatus] = useState<FrontierStatus | null>(null);
  const [settings, setSettings] = useState<FrontierSettings>(DEFAULT_SETTINGS);
  const [seeds, setSeeds] = useState('');
  const [priority, setPriority] = useState('');
  const [skipInput, setSkipInput] = useState('');
  const [allowInput, setAllowInput] = useState('');
  const [denyInput, setDenyInput] = useState('');
  const [feedInput, setFeedInput] = useState('');
  const { items: toasts, push: pushToast, dismiss: dismissToast } = useToasts();
  const [live, setLive] = useState(false);

  const snapshot = useQuery({
    queryKey: ['frontier', 'snapshot'],
    queryFn: loadFrontierSnapshot,
    enabled: open,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });

  // User-requested crawl URLs awaiting review. Loaded with the
  // snapshot when the drawer opens; decisions refetch the pending
  // list plus the status so an approved URL appears in the queue.
  const requestsQuery = useQuery({
    queryKey: ['frontier', 'crawl-requests', adminToken],
    queryFn: () => listCrawlRequests(adminToken, 'pending'),
    enabled: open,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const crawlRequests: CrawlRequest[] = requestsQuery.data?.requests ?? [];

  // Failed URLs with reasons, paginated. The page resets whenever the
  // drawer opens so a shrunk backlog cannot strand the view past the
  // last page.
  const [failedPage, setFailedPage] = useState(0);
  const failedQuery = useQuery({
    queryKey: ['frontier', 'failed', failedPage],
    queryFn: () => fetchFailedUrls(FAILED_PAGE_SIZE, failedPage * FAILED_PAGE_SIZE),
    enabled: open,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const failed: FrontierFailedList | null | undefined = failedQuery.data;
  const failedTotal = failed?.total ?? 0;
  const failedPages = Math.max(1, Math.ceil(failedTotal / FAILED_PAGE_SIZE));

  // Failed URLs rolled up by host with per-host counts. Same paging
  // rule as the URL list: the page resets whenever the drawer opens.
  const [failedDomainPage, setFailedDomainPage] = useState(0);
  const failedDomainsQuery = useQuery({
    queryKey: ['frontier', 'failed-domains', failedDomainPage],
    queryFn: () => fetchFailedDomains(FAILED_PAGE_SIZE, failedDomainPage * FAILED_PAGE_SIZE),
    enabled: open,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const failedDomains: FrontierFailedDomainList | null | undefined = failedDomainsQuery.data;
  const failedDomainTotal = failedDomains?.total ?? 0;
  const failedDomainPages = Math.max(1, Math.ceil(failedDomainTotal / FAILED_PAGE_SIZE));

  // Per-domain drill-down: expanding a row lists that host's failed
  // URLs with per-URL reasons. One expanded domain at a time; the
  // page resets whenever the selection (or the drawer) changes.
  const [expandedDomain, setExpandedDomain] = useState<string | null>(null);
  const [domainUrlPage, setDomainUrlPage] = useState(0);
  const domainUrlsQuery = useQuery({
    queryKey: ['frontier', 'failed-domain-urls', expandedDomain, domainUrlPage],
    queryFn: () =>
      expandedDomain == null
        ? Promise.resolve(null)
        : fetchFailedDomainUrls(expandedDomain, FAILED_PAGE_SIZE, domainUrlPage * FAILED_PAGE_SIZE),
    enabled: open && expandedDomain != null,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const domainUrls: FrontierFailedList | null | undefined = domainUrlsQuery.data;
  const domainUrlTotal = domainUrls?.total ?? 0;
  const domainUrlPages = Math.max(1, Math.ceil(domainUrlTotal / FAILED_PAGE_SIZE));

  function toggleDomainUrls(domain: string) {
    setExpandedDomain((current) => (current === domain ? null : domain));
    setDomainUrlPage(0);
  }

  // Same drill-down for queued hosts, tracked separately so a failed
  // row and a queued row can each hold an expansion at once.
  const [expandedQueuedDomain, setExpandedQueuedDomain] = useState<string | null>(null);
  const [queuedDomainUrlPage, setQueuedDomainUrlPage] = useState(0);
  const queuedDomainUrlsQuery = useQuery({
    queryKey: ['frontier', 'queued-domain-urls', expandedQueuedDomain, queuedDomainUrlPage],
    queryFn: () =>
      expandedQueuedDomain == null
        ? Promise.resolve(null)
        : fetchQueuedDomainUrls(
            expandedQueuedDomain,
            FAILED_PAGE_SIZE,
            queuedDomainUrlPage * FAILED_PAGE_SIZE,
          ),
    enabled: open && expandedQueuedDomain != null,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const queuedDomainUrls: FrontierFailedList | null | undefined = queuedDomainUrlsQuery.data;
  const queuedDomainUrlTotal = queuedDomainUrls?.total ?? 0;
  const queuedDomainUrlPages = Math.max(1, Math.ceil(queuedDomainUrlTotal / FAILED_PAGE_SIZE));

  function toggleQueuedDomainUrls(domain: string) {
    setExpandedQueuedDomain((current) => (current === domain ? null : domain));
    setQueuedDomainUrlPage(0);
  }

  // Queued URLs rolled up by host with per-host counts. Same paging
  // rule: the page resets whenever the drawer opens.
  const [queuedDomainPage, setQueuedDomainPage] = useState(0);
  const queuedDomainsQuery = useQuery({
    queryKey: ['frontier', 'queued-domains', queuedDomainPage],
    queryFn: () => fetchQueuedDomains(FAILED_PAGE_SIZE, queuedDomainPage * FAILED_PAGE_SIZE),
    enabled: open,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  const queuedDomains: FrontierQueuedDomainList | null | undefined = queuedDomainsQuery.data;
  const queuedDomainTotal = queuedDomains?.total ?? 0;
  const queuedDomainPages = Math.max(1, Math.ceil(queuedDomainTotal / FAILED_PAGE_SIZE));

  useEffect(() => {
    if (open) {
      setFailedPage(0);
      setFailedDomainPage(0);
      setQueuedDomainPage(0);
      setExpandedDomain(null);
      setDomainUrlPage(0);
      setExpandedQueuedDomain(null);
      setQueuedDomainUrlPage(0);
    }
  }, [open]);

  const [workers, setWorkers] = useState<FrontierWorker[]>([]);

  useEffect(() => {
    if (!snapshot.data) return;
    if (snapshot.data.status) {
      setStatus(snapshot.data.status);
    } else {
      pushToast('Frontier status unavailable.', 'error');
    }
    setSettings(snapshot.data.settings);
    setWorkers(snapshot.data.workers);
  }, [snapshot.data]);

  useEffect(() => {
    if (snapshot.isError && !snapshot.data) {
      pushToast('Frontier status unavailable.', 'error');
    }
  }, [snapshot.isError, snapshot.data]);

  // Live stats: the backend pushes the status view over a socket, so
  // the queue/checkpoint/workers stay fresh without polling. Any
  // socket or frame failure degrades to the snapshot above plus the
  // manual Refresh button (the `live` badge shows which is in play).
  useEffect(() => {
    if (!open) return;
    let socket: WebSocket | null = null;
    let closed = false;
    try {
      // Browsers cannot set WS headers, so the token travels as a
      // subprotocol the backend gate accepts (see `_ws_admin_denied`).
      const protocols = adminToken.trim() ? ['x-admin-token', adminToken.trim()] : [];
      socket = new WebSocket(frontierLiveUrl(), protocols);
    } catch {
      return;
    }
    socket.onopen = () => {
      if (!closed) setLive(true);
    };
    socket.onmessage = (event: MessageEvent) => {
      if (closed) return;
      try {
        const data: unknown = JSON.parse(String(event.data));
        if (isFrontierStatus(data)) {
          // The socket skips the lag probe by design (GET only), so its
          // nulls mean "no fresh probe", not "lag unknown": keep the
          // last GET values instead of flashing back to unknown.
          setStatus((prev) =>
            prev && data.kafka_lag_total == null
              ? {
                  ...data,
                  kafka_lag_total: prev.kafka_lag_total,
                  kafka_lag_partitions: prev.kafka_lag_partitions,
                }
              : data,
          );
        }
      } catch {
        /* malformed frames never break the dashboard */
      }
    };
    const markStale = () => {
      if (!closed) setLive(false);
    };
    socket.onerror = markStale;
    socket.onclose = markStale;
    return () => {
      closed = true;
      socket?.close();
    };
  }, [open, adminToken]);

  const loading = snapshot.isPending;

  async function refresh() {
    const [result, requestsResult] = await Promise.all([
      snapshot.refetch(),
      requestsQuery.refetch(),
    ]);
    if (result.isError || requestsResult.isError) {
      pushToast('Refresh failed.', 'error');
    }
  }

  async function handleDecideCrawlRequest(id: number, decision: 'approve' | 'decline') {
    try {
      const decided = await decideCrawlRequest(id, decision, adminToken);
      pushToast(
        decision === 'approve'
          ? `Approved — ${decided.url} queued for crawling.`
          : `Declined — ${decided.url} will not be crawled.`,
        'success',
      );
      await Promise.all([requestsQuery.refetch(), snapshot.refetch()]);
    } catch {
      pushToast(decision === 'approve' ? 'Approve failed.' : 'Decline failed.', 'error');
    }
  }

  function handleToggle() {
    setOpen((next) => !next);
  }

  async function handlePause() {
    try {
      setStatus(await pauseFrontier(fetch, adminToken));
      pushToast('Paused.', 'success');
    } catch {
      pushToast('Pause failed.', 'error');
    }
  }

  async function handleClear() {
    if (
      !window.confirm(
        'Pause crawling and permanently drop all queued URLs? In-flight and history are kept.',
      )
    ) {
      return;
    }
    try {
      const cleared = await clearFrontier(fetch, adminToken);
      setStatus(cleared);
      const count = cleared.cleared_queued ?? 0;
      const topic = cleared.kafka_purged
        ? 'Topic purged.'
        : 'Topic not purged: already-published URLs may refill the queue on resume.';
      pushToast(`Paused. Cleared ${count} queued URL(s). ${topic}`, 'success');
    } catch {
      pushToast('Clear failed.', 'error');
    }
  }

  async function handleResume() {
    try {
      const list = seeds
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean);
      const trimmed = priority.trim();
      const parsed = trimmed === '' ? undefined : Number(trimmed);
      if (parsed !== undefined && !Number.isInteger(parsed)) {
        pushToast('Priority must be a whole number.', 'error');
        return;
      }
      setStatus(await resumeFrontier(list, fetch, adminToken, parsed));
      pushToast(list.length > 0 ? `Resumed with ${list.length} seed(s).` : 'Resumed.', 'success');
      setSeeds('');
      setPriority('');
    } catch {
      pushToast('Resume failed.', 'error');
    }
  }

  const DOMAIN_OP_LABEL: Record<FrontierDomainOp, string> = {
    requeue: 'Requeued',
    purge: 'Flushed',
    skip: 'Skipped',
  };

  async function refreshFailed() {
    await Promise.all([
      failedQuery.refetch(),
      failedDomainsQuery.refetch(),
      queuedDomainsQuery.refetch(),
      snapshot.refetch(),
    ]);
  }

  async function handleDomainAction(domain: string, op: FrontierDomainOp) {
    if (
      op === 'purge' &&
      !window.confirm(
        `Permanently delete all failed history for ${domain}? Queued, active, and done rows are kept.`,
      )
    ) {
      return;
    }
    try {
      const result = await domainAction(op, [domain], adminToken);
      pushToast(`${DOMAIN_OP_LABEL[op]} — ${domain}: ${result.affected} row(s).`, 'success');
      await refreshFailed();
    } catch {
      pushToast(`${DOMAIN_OP_LABEL[op]} failed.`, 'error');
    }
  }

  async function handleBlockDomain(domain: string) {
    if (
      !window.confirm(
        `Add ${domain} to deny_domains? Its queued rows keep their policy gate immediately; workers pick the config up on refresh.`,
      )
    ) {
      return;
    }
    try {
      await blockDomains([domain], adminToken);
      const denied = [...(settings.deny_domains ?? [])];
      if (!denied.includes(domain)) denied.push(domain);
      const saved = await saveFrontierSettings(
        { ...settings, deny_domains: denied },
        fetch,
        adminToken,
      );
      setSettings(saved.settings);
      pushToast(`Blocked — ${domain} added to deny_domains.`, 'success');
      await refreshFailed();
    } catch {
      pushToast('Block failed.', 'error');
    }
  }

  async function handleAutoTriage() {
    if (
      !window.confirm(
        'Auto-triage every failed domain by its top reason? ' +
          'Robots-disallowed, content-type, 404/401 are skipped and flushed; ' +
          '429 and timeouts are requeued with backoff; thin content is also blocked. ' +
          'Unknown reasons are left alone.',
      )
    ) {
      return;
    }
    try {
      const result = await autoTriageFailedDomains(false, adminToken);
      const counts: Record<string, number> = {};
      for (const entry of result.triaged) {
        counts[entry.action] = (counts[entry.action] ?? 0) + 1;
      }
      const summary = Object.entries(counts)
        .map(([action, n]) => `${n} ${action}`)
        .join(', ');
      pushToast(
        `Auto-triage done — ${result.triaged.length} domain(s): ${summary || 'none'}.`,
        'success',
      );
      await refreshFailed();
    } catch {
      pushToast('Auto-triage failed.', 'error');
    }
  }

  async function handleQueuedPriority(domain: string, delta: number) {
    try {
      const result = await bumpDomainPriority([domain], delta, adminToken);
      pushToast(
        `${delta > 0 ? 'Priority raised' : 'Priority lowered'} — ${domain}: ${result.affected} row(s).`,
        'success',
      );
      await queuedDomainsQuery.refetch();
    } catch {
      pushToast('Priority change failed.', 'error');
    }
  }

  async function handleSaveSettings() {
    try {
      const saved = await saveFrontierSettings(settings, fetch, adminToken);
      setSettings(saved.settings);
      pushToast('Settings saved.', 'success');
    } catch {
      pushToast('Saving settings failed.', 'error');
    }
  }

  function normalizeSkipDomain(raw: string): string {
    const withoutScheme = raw
      .trim()
      .toLowerCase()
      .replace(/^[a-z][a-z0-9+.-]*:\/\//, '');
    const host = withoutScheme.split('/')[0].replace(/^\*\./, '').split(':')[0].trim();
    return host;
  }

  function handleAddSkipDomain() {
    const domain = normalizeSkipDomain(skipInput);
    if (!domain) {
      pushToast('Enter a domain to skip.', 'error');
      return;
    }
    if ((settings.skip_domains ?? []).includes(domain)) {
      pushToast('Domain is already skipped.', 'error');
      return;
    }
    setSettings({ ...settings, skip_domains: [...(settings.skip_domains ?? []), domain] });
    setSkipInput('');
  }

  function handleRemoveSkipDomain(domain: string) {
    setSettings({
      ...settings,
      skip_domains: (settings.skip_domains ?? []).filter((entry) => entry !== domain),
    });
  }

  function handleAddAllowDomain() {
    const domain = normalizeSkipDomain(allowInput);
    if (!domain) {
      pushToast('Enter a domain to allow.', 'error');
      return;
    }
    if ((settings.allow_domains ?? []).includes(domain)) {
      pushToast('Domain is already allowed.', 'error');
      return;
    }
    setSettings({ ...settings, allow_domains: [...(settings.allow_domains ?? []), domain] });
    setAllowInput('');
  }

  function handleRemoveAllowDomain(domain: string) {
    setSettings({
      ...settings,
      allow_domains: (settings.allow_domains ?? []).filter((entry) => entry !== domain),
    });
  }

  function handleAddDenyDomain() {
    const domain = normalizeSkipDomain(denyInput);
    if (!domain) {
      pushToast('Enter a domain to block.', 'error');
      return;
    }
    if ((settings.deny_domains ?? []).includes(domain)) {
      pushToast('Domain is already blocked.', 'error');
      return;
    }
    setSettings({ ...settings, deny_domains: [...(settings.deny_domains ?? []), domain] });
    setDenyInput('');
  }

  function handleRemoveDenyDomain(domain: string) {
    setSettings({
      ...settings,
      deny_domains: (settings.deny_domains ?? []).filter((entry) => entry !== domain),
    });
  }

  function normalizeFeedUrl(raw: string): string {
    const trimmed = raw.trim();
    if (!trimmed) return '';
    try {
      const parsed = new URL(trimmed.includes('://') ? trimmed : `https://${trimmed}`);
      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return '';
      return parsed.toString();
    } catch {
      return '';
    }
  }

  function handleAddFeed() {
    const feed = normalizeFeedUrl(feedInput);
    if (!feed) {
      pushToast('Enter a valid http(s) feed or sitemap URL.', 'error');
      return;
    }
    if ((settings.discovery_feeds ?? []).includes(feed)) {
      pushToast('Feed is already added.', 'error');
      return;
    }
    setSettings({ ...settings, discovery_feeds: [...(settings.discovery_feeds ?? []), feed] });
    setFeedInput('');
  }

  function handleRemoveFeed(feed: string) {
    setSettings({
      ...settings,
      discovery_feeds: (settings.discovery_feeds ?? []).filter((entry) => entry !== feed),
    });
  }

  const queue = status ? readFrontierQueue(status) : [];
  const newUrls = status ? readFrontierNewUrls(status) : [];
  const checkpoint = status?.checkpoint;
  const discovery = status ? readFrontierDiscovery(status) : null;
  const kafkaEnabled = status?.kafka_enabled === true;
  const kafkaLag = typeof status?.kafka_lag_total === 'number' ? status.kafka_lag_total : null;

  return (
    <GlassPanel variant="rail">
      <Drawer
        triggerLabel={open ? 'Hide frontier' : 'Show frontier'}
        open={open}
        onToggle={() => void handleToggle()}
        panelId="frontier-panel"
        panelTestId="frontier-panel"
      >
        {loading ? (
          <p>Loading frontier…</p>
        ) : (
          <>
            <section className="ol-dash-group" aria-label="Fleet status">
              <h3 className="ol-dash-title">Fleet status</h3>
              <p data-testid="frontier-state" className="my-ol-s">
                {status?.paused ? <Badge>Paused</Badge> : <Badge>Running</Badge>}
              </p>

              <div className="flex flex-wrap gap-ol-s my-ol-s">
                <Button variant="secondary" onClick={() => void handlePause()}>
                  Pause
                </Button>{' '}
                <Button variant="secondary" onClick={() => void handleResume()}>
                  Resume
                </Button>{' '}
                <Button variant="ghost" onClick={() => void refresh()}>
                  Refresh
                </Button>{' '}
                <Button variant="secondary" onClick={() => void handleClear()}>
                  Pause &amp; clear
                </Button>{' '}
                <span data-testid="frontier-live">
                  <Badge>{live ? 'Live' : 'Snapshot'}</Badge>
                </span>
              </div>

              <h4 className="mt-ol-m mb-ol-s text-ol-m">Checkpoint</h4>
              <p data-testid="frontier-checkpoint" className="my-ol-s">
                {checkpoint
                  ? `${checkpoint.phase} · ${checkpoint.pages_indexed}/${checkpoint.pages_attempted} pages`
                  : 'No checkpoint yet'}
              </p>

              <h4 className="mt-ol-m mb-ol-s text-ol-m">Crawl diagnostics</h4>
              <p data-testid="frontier-kafka-lag" className="my-ol-s text-ol-s">
                {kafkaEnabled
                  ? kafkaLag === null
                    ? 'Kafka lag: unknown — probe failed'
                    : `Kafka lag: ${kafkaLag} urls`
                  : 'Kafka lag: disabled — store-backed queue'}
              </p>
              <p data-testid="frontier-discovery" className="my-ol-s text-ol-s">
                {discovery
                  ? `Discovery: ${discovery.feeds_checked} feeds · ${discovery.new_urls} new · ${discovery.refreshed} refreshed`
                  : 'Discovery: no pass reported yet'}
              </p>

              <h4 className="mt-ol-m mb-ol-s text-ol-m">Workers ({workers.length})</h4>
              {workers.length === 0 ? (
                <p className="my-ol-s text-ol-s">No workers heartbeating</p>
              ) : (
                <>
                  {workers.some((worker) => worker.stats) && (
                    <p data-testid="frontier-throughput" className="my-ol-s text-ol-s">
                      Throughput: {formatRate(sumRates(workers, 'pages_per_min'))} pages ·{' '}
                      {formatRate(sumRates(workers, 'tokens_per_min'))} tokens ·{' '}
                      {workers.reduce((sum, worker) => sum + (worker.stats?.pages ?? 0), 0)} pages ·{' '}
                      {workers.reduce((sum, worker) => sum + (worker.stats?.tokens ?? 0), 0)} tokens
                      total
                    </p>
                  )}
                  <ul
                    data-testid="frontier-workers"
                    className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
                  >
                    {workers.map((worker) => (
                      <li key={worker.worker_id} className="break-all">
                        <Badge>{worker.status}</Badge> {worker.worker_id} · {worker.claimed} claimed
                        · crawl {worker.crawl_id}
                        {worker.stats ? (
                          <>
                            {' '}
                            · {formatRate(worker.pages_per_min)} pages,{' '}
                            {formatRate(worker.tokens_per_min)} tokens · {worker.stats.pages} pages
                            · {worker.stats.tokens} tokens total
                          </>
                        ) : null}
                        {worker.discovery ? (
                          <>
                            {' '}
                            · discovery: {worker.discovery.feeds_checked} feeds,{' '}
                            {worker.discovery.new_urls} new, {worker.discovery.refreshed} refreshed
                          </>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </section>
            <details className="ol-disclosure">
              <summary className="ol-disclosure-title">
                Queue ({status?.queue_depth ?? 0}
                {queue.filter((entry) => entry.status === 'skipped').length > 0 && (
                  <> · {queue.filter((entry) => entry.status === 'skipped').length} skipped</>
                )}
                ) · raw rows
              </summary>
              {queue.length === 0 ? (
                <p className="my-ol-s text-ol-s">Queue is empty</p>
              ) : (
                <ol
                  data-testid="frontier-queue"
                  aria-label="Frontier queue"
                  className="flex min-h-0 flex-col gap-ol-xs my-ol-s pl-ol-m max-h-64 overflow-y-auto"
                >
                  {queue.map((entry) => (
                    <li key={entry.url} className="break-all">
                      <Badge>{entry.status}</Badge> {entry.url} · depth {entry.depth} · priority{' '}
                      {entry.priority ?? 0}
                    </li>
                  ))}
                </ol>
              )}
            </details>
            <details className="ol-disclosure">
              <summary className="ol-disclosure-title">New URLs ({newUrls.length})</summary>
              {newUrls.length === 0 ? (
                <p className="my-ol-s text-ol-s">No new URLs</p>
              ) : (
                <ul
                  data-testid="frontier-new-urls"
                  className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
                >
                  {newUrls.map((entry) => (
                    <li key={entry.url} className="break-all">
                      <Badge>{entry.status}</Badge> {entry.url}
                    </li>
                  ))}
                </ul>
              )}
            </details>
            <section className="ol-dash-group" aria-label="Needs attention">
              <h3 className="ol-dash-title">Needs attention</h3>
              <h4 className="mt-ol-m mb-ol-s text-ol-m">Failed URLs ({failedTotal})</h4>
              {failedQuery.isPending ? (
                <p className="my-ol-s text-ol-s">Loading failed URLs…</p>
              ) : failed == null || failed.urls.length === 0 ? (
                <p data-testid="frontier-failed-empty" className="my-ol-s text-ol-s">
                  No failed URLs
                </p>
              ) : (
                <>
                  <ul
                    data-testid="frontier-failed"
                    className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
                  >
                    {failed.urls.map((entry) => (
                      <li key={entry.url} className="break-all">
                        <Badge>{entry.attempts ?? 0} tries</Badge> {entry.url}
                        {entry.reason ? <span className="text-ol-s"> · {entry.reason}</span> : null}
                      </li>
                    ))}
                  </ul>
                  <div className="flex items-center gap-ol-s my-ol-s">
                    <Button
                      variant="ghost"
                      data-testid="frontier-failed-prev"
                      onClick={() => setFailedPage((page) => Math.max(0, page - 1))}
                      disabled={failedPage <= 0}
                    >
                      Previous
                    </Button>
                    <span data-testid="frontier-failed-page" className="text-ol-s">
                      Page {failedPage + 1} of {failedPages}
                    </span>
                    <Button
                      variant="ghost"
                      data-testid="frontier-failed-next"
                      onClick={() => setFailedPage((page) => Math.min(failedPages - 1, page + 1))}
                      disabled={failedPage >= failedPages - 1}
                    >
                      Next
                    </Button>
                  </div>
                </>
              )}

              <h4 className="mt-ol-m mb-ol-s text-ol-m">Failed domains ({failedDomainTotal})</h4>
              {failedDomainTotal > 0 ? (
                <Button
                  variant="secondary"
                  data-testid="frontier-auto-triage"
                  onClick={() => void handleAutoTriage()}
                >
                  Auto-triage
                </Button>
              ) : null}
              {failedDomainsQuery.isPending ? (
                <p className="my-ol-s text-ol-s">Loading failed domains…</p>
              ) : failedDomains == null || failedDomains.domains.length === 0 ? (
                <p data-testid="frontier-failed-domains-empty" className="my-ol-s text-ol-s">
                  No failed domains
                </p>
              ) : (
                <>
                  <ul
                    data-testid="frontier-failed-domains"
                    className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
                  >
                    {failedDomains.domains.map((entry) => (
                      <li key={entry.domain} className="break-all ol-domain-row">
                        <Badge>{entry.failed}</Badge>{' '}
                        <span className="ol-domain-name">{entry.domain}</span>{' '}
                        <span className="text-ol-s">{entry.top_reason}</span>
                        <Button
                          variant="ghost"
                          data-testid={`frontier-domain-toggle-${entry.domain}`}
                          onClick={() => toggleDomainUrls(entry.domain)}
                        >
                          {expandedDomain === entry.domain ? 'Hide URLs' : 'Show URLs'}
                        </Button>{' '}
                        <Button
                          variant="secondary"
                          data-testid={`frontier-domain-requeue-${entry.domain}`}
                          onClick={() => void handleDomainAction(entry.domain, 'requeue')}
                        >
                          Requeue
                        </Button>{' '}
                        <Button
                          variant="secondary"
                          data-testid={`frontier-domain-flush-${entry.domain}`}
                          onClick={() => void handleDomainAction(entry.domain, 'purge')}
                        >
                          Flush
                        </Button>{' '}
                        <Button
                          variant="ghost"
                          data-testid={`frontier-domain-skip-${entry.domain}`}
                          onClick={() => void handleDomainAction(entry.domain, 'skip')}
                        >
                          Skip
                        </Button>{' '}
                        <Button
                          variant="ghost"
                          data-testid={`frontier-domain-block-${entry.domain}`}
                          onClick={() => void handleBlockDomain(entry.domain)}
                        >
                          Block
                        </Button>
                        {expandedDomain === entry.domain ? (
                          <div className="my-ol-s">
                            {domainUrlsQuery.isPending ? (
                              <p className="text-ol-s">Loading URLs…</p>
                            ) : domainUrls == null || domainUrls.urls.length === 0 ? (
                              <p
                                data-testid={`frontier-domain-urls-empty-${entry.domain}`}
                                className="text-ol-s"
                              >
                                No failed URLs for this domain
                              </p>
                            ) : (
                              <>
                                <ul
                                  data-testid={`frontier-domain-urls-${entry.domain}`}
                                  className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
                                >
                                  {domainUrls.urls.map((failed) => (
                                    <li key={failed.url} className="break-all">
                                      <Badge>{failed.attempts}</Badge> {failed.url}{' '}
                                      <span className="text-ol-s">· {failed.reason}</span>
                                    </li>
                                  ))}
                                </ul>
                                <div className="flex items-center gap-ol-s my-ol-s">
                                  <Button
                                    variant="ghost"
                                    data-testid={`frontier-domain-urls-prev-${entry.domain}`}
                                    onClick={() =>
                                      setDomainUrlPage((page) => Math.max(0, page - 1))
                                    }
                                    disabled={domainUrlPage <= 0}
                                  >
                                    Previous
                                  </Button>
                                  <span className="text-ol-s">
                                    Page {domainUrlPage + 1} of {domainUrlPages}
                                  </span>
                                  <Button
                                    variant="ghost"
                                    data-testid={`frontier-domain-urls-next-${entry.domain}`}
                                    onClick={() =>
                                      setDomainUrlPage((page) =>
                                        Math.min(domainUrlPages - 1, page + 1),
                                      )
                                    }
                                    disabled={domainUrlPage >= domainUrlPages - 1}
                                  >
                                    Next
                                  </Button>
                                </div>
                              </>
                            )}
                          </div>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                  <div className="flex items-center gap-ol-s my-ol-s">
                    <Button
                      variant="ghost"
                      data-testid="frontier-failed-domains-prev"
                      onClick={() => setFailedDomainPage((page) => Math.max(0, page - 1))}
                      disabled={failedDomainPage <= 0}
                    >
                      Previous
                    </Button>
                    <span data-testid="frontier-failed-domains-page" className="text-ol-s">
                      Page {failedDomainPage + 1} of {failedDomainPages}
                    </span>
                    <Button
                      variant="ghost"
                      data-testid="frontier-failed-domains-next"
                      onClick={() =>
                        setFailedDomainPage((page) => Math.min(failedDomainPages - 1, page + 1))
                      }
                      disabled={failedDomainPage >= failedDomainPages - 1}
                    >
                      Next
                    </Button>
                  </div>
                </>
              )}
            </section>
            <section className="ol-dash-group" aria-label="Queued by domain">
              <h3 className="ol-dash-title">Queued by domain</h3>
              <h4 className="mt-ol-m mb-ol-s text-ol-m">Queued domains ({queuedDomainTotal})</h4>
              {queuedDomainsQuery.isPending ? (
                <p className="my-ol-s text-ol-s">Loading queued domains…</p>
              ) : queuedDomains == null || queuedDomains.domains.length === 0 ? (
                <p data-testid="frontier-queued-domains-empty" className="my-ol-s text-ol-s">
                  No queued domains
                </p>
              ) : (
                <>
                  <ul
                    data-testid="frontier-queued-domains"
                    className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
                  >
                    {queuedDomains.domains.map((entry) => (
                      <li key={entry.domain} className="break-all ol-domain-row">
                        <Badge>{entry.queued}</Badge> <Badge>p{entry.top_priority}</Badge>{' '}
                        <span className="ol-domain-name">{entry.domain}</span>
                        <Button
                          variant="ghost"
                          data-testid={`frontier-queued-toggle-${entry.domain}`}
                          onClick={() => toggleQueuedDomainUrls(entry.domain)}
                        >
                          {expandedQueuedDomain === entry.domain ? 'Hide URLs' : 'Show URLs'}
                        </Button>{' '}
                        <Button
                          variant="secondary"
                          data-testid={`frontier-queued-prio-up-${entry.domain}`}
                          onClick={() => void handleQueuedPriority(entry.domain, 1)}
                        >
                          +1 priority
                        </Button>{' '}
                        <Button
                          variant="secondary"
                          data-testid={`frontier-queued-prio-down-${entry.domain}`}
                          onClick={() => void handleQueuedPriority(entry.domain, -1)}
                        >
                          −1 priority
                        </Button>{' '}
                        <Button
                          variant="ghost"
                          data-testid={`frontier-queued-skip-${entry.domain}`}
                          onClick={() => void handleDomainAction(entry.domain, 'skip')}
                        >
                          Skip
                        </Button>{' '}
                        <Button
                          variant="ghost"
                          data-testid={`frontier-queued-block-${entry.domain}`}
                          onClick={() => void handleBlockDomain(entry.domain)}
                        >
                          Block
                        </Button>
                        {expandedQueuedDomain === entry.domain ? (
                          <div
                            data-testid={`frontier-queued-urls-${entry.domain}`}
                            className="ml-ol-m mt-ol-xs border-l border-ol-border pl-ol-s"
                          >
                            {queuedDomainUrlsQuery.isLoading ? (
                              <p className="text-ol-s">Loading URLs…</p>
                            ) : queuedDomainUrls == null ? (
                              <p className="text-ol-s">Could not load URLs.</p>
                            ) : queuedDomainUrls.urls.length === 0 ? (
                              <p className="text-ol-s">No queued URLs for this domain.</p>
                            ) : (
                              <>
                                <ul className="flex min-h-0 flex-col gap-ol-xs my-ol-xs max-h-64 overflow-y-auto">
                                  {queuedDomainUrls.urls.map((item) => (
                                    <li key={item.url} className="break-all">
                                      {(item.attempts ?? 0) > 0 ? (
                                        <Badge>{item.attempts}</Badge>
                                      ) : null}{' '}
                                      {item.url}
                                      {item.reason != null && item.reason !== '' ? (
                                        <span className="text-ol-s"> · {item.reason}</span>
                                      ) : null}
                                    </li>
                                  ))}
                                </ul>
                                <div className="flex items-center gap-ol-s my-ol-xs">
                                  <Button
                                    variant="ghost"
                                    data-testid={`frontier-queued-urls-prev-${entry.domain}`}
                                    onClick={() =>
                                      setQueuedDomainUrlPage((page) => Math.max(0, page - 1))
                                    }
                                    disabled={queuedDomainUrlPage <= 0}
                                  >
                                    Previous
                                  </Button>
                                  <span
                                    data-testid={`frontier-queued-urls-page-${entry.domain}`}
                                    className="text-ol-s"
                                  >
                                    Page {queuedDomainUrlPage + 1} of {queuedDomainUrlPages}
                                  </span>
                                  <Button
                                    variant="ghost"
                                    data-testid={`frontier-queued-urls-next-${entry.domain}`}
                                    onClick={() =>
                                      setQueuedDomainUrlPage((page) =>
                                        Math.min(queuedDomainUrlPages - 1, page + 1),
                                      )
                                    }
                                    disabled={queuedDomainUrlPage >= queuedDomainUrlPages - 1}
                                  >
                                    Next
                                  </Button>
                                </div>
                              </>
                            )}
                          </div>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                  <div className="flex items-center gap-ol-s my-ol-s">
                    <Button
                      variant="ghost"
                      data-testid="frontier-queued-domains-prev"
                      onClick={() => setQueuedDomainPage((page) => Math.max(0, page - 1))}
                      disabled={queuedDomainPage <= 0}
                    >
                      Previous
                    </Button>
                    <span data-testid="frontier-queued-domains-page" className="text-ol-s">
                      Page {queuedDomainPage + 1} of {queuedDomainPages}
                    </span>
                    <Button
                      variant="ghost"
                      data-testid="frontier-queued-domains-next"
                      onClick={() =>
                        setQueuedDomainPage((page) => Math.min(queuedDomainPages - 1, page + 1))
                      }
                      disabled={queuedDomainPage >= queuedDomainPages - 1}
                    >
                      Next
                    </Button>
                  </div>
                </>
              )}
            </section>
            <section className="ol-dash-group" aria-label="Intake and settings">
              <h3 className="ol-dash-title">Intake &amp; settings</h3>
              <h4 className="mt-ol-m mb-ol-s text-ol-m">Crawl requests ({crawlRequests.length})</h4>
              {crawlRequests.length === 0 ? (
                <p className="my-ol-s text-ol-s">No URLs awaiting review</p>
              ) : (
                <ul
                  data-testid="frontier-crawl-requests"
                  aria-label="Crawl requests awaiting review"
                  className="flex min-h-0 flex-col gap-ol-xs my-ol-s pl-ol-m max-h-64 overflow-y-auto"
                >
                  {crawlRequests.map((entry) => (
                    <li key={entry.id ?? entry.url} className="break-all">
                      <span className="text-ol-s">{entry.url}</span>{' '}
                      <Button
                        variant="secondary"
                        data-testid={`crawl-request-approve-${entry.id}`}
                        onClick={() =>
                          entry.id !== null && void handleDecideCrawlRequest(entry.id, 'approve')
                        }
                        disabled={entry.id === null}
                      >
                        Approve
                      </Button>{' '}
                      <Button
                        variant="ghost"
                        data-testid={`crawl-request-decline-${entry.id}`}
                        onClick={() =>
                          entry.id !== null && void handleDecideCrawlRequest(entry.id, 'decline')
                        }
                        disabled={entry.id === null}
                      >
                        Decline
                      </Button>
                    </li>
                  ))}
                </ul>
              )}

              <details className="ol-disclosure">
                <summary className="ol-disclosure-title">Enqueue seeds</summary>
                <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                  Seed URLs (one per line)
                  <textarea
                    data-testid="frontier-seeds"
                    value={seeds}
                    onChange={(event) => setSeeds(event.target.value)}
                    rows={3}
                    className="font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                  />
                </label>
                <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                  Priority for these seeds (higher dequeues first; blank keeps stored)
                  <input
                    data-testid="frontier-priority"
                    type="number"
                    step={1}
                    value={priority}
                    onChange={(event) => setPriority(event.target.value)}
                    placeholder="0"
                    className="font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                  />
                </label>
              </details>
              <details className="ol-disclosure">
                <summary className="ol-disclosure-title">Settings</summary>
                <Card data-testid="frontier-settings">
                  <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                    Max pages
                    <input
                      data-testid="frontier-setting-max-pages"
                      type="number"
                      min={1}
                      value={settings.max_pages}
                      onChange={(event) =>
                        setSettings({ ...settings, max_pages: Number(event.target.value) })
                      }
                      className="font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                    />
                  </label>
                  <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                    Max queued URLs
                    <input
                      data-testid="frontier-setting-max-queued"
                      type="number"
                      min={1}
                      value={settings.max_queued_urls}
                      onChange={(event) =>
                        setSettings({ ...settings, max_queued_urls: Number(event.target.value) })
                      }
                      className="font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                    />
                  </label>
                  <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                    Crawl delay (s)
                    <input
                      data-testid="frontier-setting-delay"
                      type="number"
                      min={0}
                      step={0.1}
                      value={settings.crawl_delay_s}
                      onChange={(event) =>
                        setSettings({ ...settings, crawl_delay_s: Number(event.target.value) })
                      }
                      className="font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                    />
                  </label>
                  <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                    Default max depth
                    <input
                      data-testid="frontier-setting-depth"
                      type="number"
                      min={0}
                      value={settings.default_max_depth}
                      onChange={(event) =>
                        setSettings({ ...settings, default_max_depth: Number(event.target.value) })
                      }
                      className="font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                    />
                  </label>
                  <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                    Refresh pages older than (days; 0 disables)
                    <input
                      data-testid="frontier-setting-refresh-after"
                      type="number"
                      min={0}
                      step={1}
                      value={settings.refresh_after_days}
                      onChange={(event) =>
                        setSettings({ ...settings, refresh_after_days: Number(event.target.value) })
                      }
                      className="font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                    />
                  </label>
                  <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                    Discovery pass interval (s; 0 disables)
                    <input
                      data-testid="frontier-setting-discovery-interval"
                      type="number"
                      min={0}
                      step={60}
                      value={settings.discovery_interval_s}
                      onChange={(event) =>
                        setSettings({
                          ...settings,
                          discovery_interval_s: Number(event.target.value),
                        })
                      }
                      className="font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                    />
                  </label>
                  <label className="flex items-center gap-ol-xs text-ol-s font-semibold">
                    <input
                      data-testid="frontier-setting-robots"
                      type="checkbox"
                      checked={settings.respect_robots}
                      onChange={(event) =>
                        setSettings({ ...settings, respect_robots: event.target.checked })
                      }
                    />{' '}
                    Respect robots.txt
                  </label>
                  <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                    Crawler User-Agent
                    <input
                      data-testid="frontier-setting-user-agent"
                      type="text"
                      maxLength={256}
                      value={settings.user_agent}
                      onChange={(event) =>
                        setSettings({ ...settings, user_agent: event.target.value })
                      }
                      className="font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                    />
                  </label>
                  <div className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                    Skip domains (queued URLs from these are never fetched)
                    <div className="flex gap-ol-xs">
                      <input
                        data-testid="frontier-skip-input"
                        type="text"
                        aria-label="Domain to skip"
                        value={skipInput}
                        onChange={(event) => setSkipInput(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter') handleAddSkipDomain();
                        }}
                        className="flex-1 font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                      />
                      <Button variant="secondary" onClick={handleAddSkipDomain}>
                        Add
                      </Button>
                    </div>
                    {(settings.skip_domains ?? []).length > 0 && (
                      <ul data-testid="frontier-skip-list" className="flex flex-wrap gap-ol-xs">
                        {(settings.skip_domains ?? []).map((domain) => (
                          <li key={domain}>
                            <Badge>{domain}</Badge>{' '}
                            <Button
                              variant="ghost"
                              data-testid={`frontier-skip-remove-${domain}`}
                              onClick={() => handleRemoveSkipDomain(domain)}
                            >
                              ×
                            </Button>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                  <div className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                    Allow domains (when set, only these are ever fetched)
                    <div className="flex gap-ol-xs">
                      <input
                        data-testid="frontier-allow-input"
                        type="text"
                        aria-label="Domain to allow"
                        value={allowInput}
                        onChange={(event) => setAllowInput(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter') handleAddAllowDomain();
                        }}
                        className="flex-1 font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                      />
                      <Button variant="secondary" onClick={handleAddAllowDomain}>
                        Add
                      </Button>
                    </div>
                    {(settings.allow_domains ?? []).length > 0 && (
                      <ul data-testid="frontier-allow-list" className="flex flex-wrap gap-ol-xs">
                        {(settings.allow_domains ?? []).map((domain) => (
                          <li key={domain}>
                            <Badge>{domain}</Badge>{' '}
                            <Button
                              variant="ghost"
                              data-testid={`frontier-allow-remove-${domain}`}
                              onClick={() => handleRemoveAllowDomain(domain)}
                            >
                              ×
                            </Button>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                  <div className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                    Block domains (queued URLs from these are never fetched)
                    <div className="flex gap-ol-xs">
                      <input
                        data-testid="frontier-deny-input"
                        type="text"
                        aria-label="Domain to block"
                        value={denyInput}
                        onChange={(event) => setDenyInput(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter') handleAddDenyDomain();
                        }}
                        className="flex-1 font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                      />
                      <Button variant="secondary" onClick={handleAddDenyDomain}>
                        Add
                      </Button>
                    </div>
                    {(settings.deny_domains ?? []).length > 0 && (
                      <ul data-testid="frontier-deny-list" className="flex flex-wrap gap-ol-xs">
                        {(settings.deny_domains ?? []).map((domain) => (
                          <li key={domain}>
                            <Badge>{domain}</Badge>{' '}
                            <Button
                              variant="ghost"
                              data-testid={`frontier-deny-remove-${domain}`}
                              onClick={() => handleRemoveDenyDomain(domain)}
                            >
                              ×
                            </Button>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                  <div className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                    Discovery feeds / sitemaps (new URLs are queued from these)
                    <div className="flex gap-ol-xs">
                      <input
                        data-testid="frontier-feed-input"
                        type="text"
                        aria-label="Feed or sitemap URL"
                        value={feedInput}
                        onChange={(event) => setFeedInput(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter') handleAddFeed();
                        }}
                        className="flex-1 font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                      />
                      <Button variant="secondary" onClick={handleAddFeed}>
                        Add
                      </Button>
                    </div>
                    {(settings.discovery_feeds ?? []).length > 0 && (
                      <ul data-testid="frontier-feed-list" className="flex flex-col gap-ol-xs">
                        {(settings.discovery_feeds ?? []).map((feed) => (
                          <li key={feed} className="flex items-center gap-ol-xs">
                            <Badge>{feed}</Badge>{' '}
                            <Button
                              variant="ghost"
                              data-testid={`frontier-feed-remove-${feed}`}
                              onClick={() => handleRemoveFeed(feed)}
                            >
                              ×
                            </Button>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                  <div className="mt-ol-s">
                    <Button variant="secondary" onClick={() => void handleSaveSettings()}>
                      Save settings
                    </Button>
                  </div>
                </Card>
              </details>
            </section>
          </>
        )}
      </Drawer>
      <Toasts items={toasts} onDismiss={dismissToast} />
    </GlassPanel>
  );
}

/**
 * Self-contained Query provider so the dashboard works wherever it is
 * mounted (admin page, tests) without app-wide wiring yet. When a
 * second screen migrates to Query, hoist this client to main.tsx.
 */
export function FrontierDrawer({ adminToken }: FrontierDrawerProps) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { retry: false, refetchOnWindowFocus: false },
        },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <FrontierDrawerView adminToken={adminToken} />
    </QueryClientProvider>
  );
}
