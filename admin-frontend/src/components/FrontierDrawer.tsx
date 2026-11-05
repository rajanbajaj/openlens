import { useEffect, useRef, useState } from 'react';
import { QueryClientProvider, useQuery } from '@tanstack/react-query';
import {
  autoTriageFailedDomains,
  blockDomains,
  bumpDomainPriority,
  clearFrontier,
  domainAction,
  enqueueSeeds,
  fetchFrontierUrlInfo,
  frontierLiveUrl,
  pauseFrontier,
  resumeFrontier,
  saveFrontierSettings,
} from '../api/client.ts';
import type { FrontierDomainOp } from '../api/client.ts';
import { createAdminQueryClient } from '../api/queryClient.tsx';
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
  FrontierSettings,
  FrontierStatus,
  FrontierUrlInfo,
  FrontierWorker,
} from '../api/types.ts';
import { CRAWLER_DEFAULTS } from '../crawlerDefaults.ts';
import { Badge } from './ui/Badge.tsx';
import { Button } from './ui/Button.tsx';
import { Card } from './ui/Card.tsx';
import { ConfirmDialog } from './ui/ConfirmDialog.tsx';
import { Drawer } from './ui/Drawer.tsx';
import { FieldError } from './ui/FieldError.tsx';
import { GlassPanel } from './ui/GlassPanel.tsx';
import { Skeleton } from './ui/Skeleton.tsx';
import { StatusBadge } from './ui/StatusBadge.tsx';
import { Toasts, useToasts } from './ui/Toast.tsx';
import { CrawlRequests } from './frontier/CrawlRequests.tsx';
import { DomainTables } from './frontier/DomainTables.tsx';
import { WorkersTable } from './frontier/WorkersTable.tsx';

/** Local defaults used before the backend settings load. */
const DEFAULT_SETTINGS: FrontierSettings = {
  max_pages: 100,
  max_queued_urls: CRAWLER_DEFAULTS.max_queued_urls,
  crawl_delay_s: 0.5,
  default_max_depth: CRAWLER_DEFAULTS.default_max_depth,
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
 * failure degrades to an inline error panel with retry instead of
 * crashing the page. Destructive submissions confirm via dialog.
 */
function FrontierDrawerView({ adminToken }: FrontierDrawerProps) {
  // Open by default: in the admin app this is a page, not a rail drawer.
  const [open, setOpen] = useState(true);
  const [status, setStatus] = useState<FrontierStatus | null>(null);
  const [settings, setSettings] = useState<FrontierSettings>(DEFAULT_SETTINGS);
  const [savedSettings, setSavedSettings] = useState<FrontierSettings>(DEFAULT_SETTINGS);
  const [seeds, setSeeds] = useState('');
  const [priority, setPriority] = useState('');
  const [priorityError, setPriorityError] = useState<string | null>(null);
  const [depth, setDepth] = useState('');
  const [depthError, setDepthError] = useState<string | null>(null);
  const [inspectUrl, setInspectUrl] = useState('');
  const [inspectResult, setInspectResult] = useState<FrontierUrlInfo | null>(null);
  const [inspectBusy, setInspectBusy] = useState(false);
  const [inspectError, setInspectError] = useState<string | null>(null);
  const [skipInput, setSkipInput] = useState('');
  const [allowInput, setAllowInput] = useState('');
  const [denyInput, setDenyInput] = useState('');
  const [feedInput, setFeedInput] = useState('');
  const [confirm, setConfirm] = useState<{ kind: 'clear' | 'save' } | null>(null);
  const { items: toasts, push: pushToast, dismiss: dismissToast } = useToasts();
  const [live, setLive] = useState(false);
  const liveRef = useRef(false);

  const snapshot = useQuery({
    queryKey: ['frontier', 'snapshot'],
    queryFn: loadFrontierSnapshot,
    enabled: open,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });

  const [workers, setWorkers] = useState<FrontierWorker[]>([]);

  useEffect(() => {
    if (!snapshot.data) return;
    if (snapshot.data.status) {
      setStatus(snapshot.data.status);
    }
    setSettings(snapshot.data.settings);
    setSavedSettings(snapshot.data.settings);
    setWorkers(snapshot.data.workers);
  }, [snapshot.data]);

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

  // Live→Snapshot flip toasts once so a dropped feed is visible.
  useEffect(() => {
    if (liveRef.current && !live && open) {
      pushToast('Live feed lost — showing snapshot…', 'info');
    }
    liveRef.current = live;
  }, [live, open, pushToast]);

  const loading = snapshot.isPending;
  const snapshotFailed = snapshot.isError && !snapshot.data;

  async function refresh() {
    const result = await snapshot.refetch();
    if (result.isError) {
      pushToast('Refresh failed.', 'error');
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
    setConfirm(null);
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

  function parsePriority(): { value?: number; error: string | null } {
    const trimmed = priority.trim();
    if (trimmed === '') return { value: undefined, error: null };
    const parsed = Number(trimmed);
    if (!Number.isInteger(parsed) || parsed < 0) {
      return { value: undefined, error: 'Priority must be a whole number (0 or more).' };
    }
    return { value: parsed, error: null };
  }

  function parseDepth(): { value?: number; error: string | null } {
    const trimmed = depth.trim();
    if (trimmed === '') return { value: undefined, error: null };
    const parsed = Number(trimmed);
    if (!Number.isInteger(parsed) || parsed < 0) {
      return { value: undefined, error: 'Depth must be a whole number (0 or more).' };
    }
    return { value: parsed, error: null };
  }

  async function handleResume() {
    const { value: parsed, error } = parsePriority();
    setPriorityError(error);
    if (error) {
      pushToast('Priority must be a whole number.', 'error');
      return;
    }
    try {
      const list = seeds
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean);
      setStatus(await resumeFrontier(list, fetch, adminToken, parsed));
      pushToast(list.length > 0 ? `Resumed with ${list.length} seed(s).` : 'Resumed.', 'success');
      setSeeds('');
      setPriority('');
    } catch {
      pushToast('Resume failed.', 'error');
    }
  }

  async function handleEnqueueSeeds() {
    const { value: parsed, error } = parsePriority();
    setPriorityError(error);
    const { value: parsedDepth, error: depthError } = parseDepth();
    setDepthError(depthError);
    if (error) {
      pushToast('Priority must be a whole number.', 'error');
      return;
    }
    if (depthError) {
      pushToast('Depth must be a whole number.', 'error');
      return;
    }
    try {
      const list = seeds
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean);
      if (list.length === 0) {
        pushToast('Add at least one seed URL first.', 'error');
        return;
      }
      const accepted = await enqueueSeeds(list, fetch, adminToken, parsed, parsedDepth);
      const skipped = accepted.skipped_over_cap ?? 0;
      pushToast(
        `Enqueued ${accepted.enqueued} seed(s) — crawlers pick them up without interruption.` +
          (skipped > 0 ? ` ${skipped} skipped: queue is at the Max queued URLs cap.` : ''),
        'success',
      );
      setSeeds('');
      setPriority('');
      setDepth('');
      await snapshot.refetch();
    } catch {
      pushToast('Enqueue seeds failed.', 'error');
    }
  }

  async function handleInspectUrl() {
    const target = inspectUrl.trim();
    if (target === '' || inspectBusy) return;
    setInspectBusy(true);
    setInspectError(null);
    try {
      const info = await fetchFrontierUrlInfo(target, fetch);
      setInspectResult(info);
      if (info === null) setInspectError('Could not load URL info.');
    } catch {
      setInspectResult(null);
      setInspectError('Could not load URL info.');
    } finally {
      setInspectBusy(false);
    }
  }

  const DOMAIN_OP_LABEL: Record<FrontierDomainOp, string> = {
    requeue: 'Requeued',
    purge: 'Flushed',
    skip: 'Skipped',
  };

  /** Single or bulk domain action with the shared affected-count toast. */
  async function handleDomainAction(op: FrontierDomainOp, domains: string[]): Promise<void> {
    const result = await domainAction(op, domains, adminToken);
    pushToast(
      `${DOMAIN_OP_LABEL[op]} — ${domains.join(', ')}: ${result.affected} row(s).`,
      'success',
    );
  }

  /** Block without its own toast: the caller confirms and announces. */
  async function handleBlockDomain(domain: string): Promise<void> {
    await blockDomains([domain], adminToken);
    const denied = [...(settings.deny_domains ?? [])];
    if (!denied.includes(domain)) denied.push(domain);
    const saved = await saveFrontierSettings(
      { ...settings, deny_domains: denied },
      fetch,
      adminToken,
    );
    setSettings(saved.settings);
    setSavedSettings(saved.settings);
  }

  async function handleAutoTriage(): Promise<void> {
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
  }

  async function handleQueuedPriority(domain: string, delta: number): Promise<void> {
    const result = await bumpDomainPriority([domain], delta, adminToken);
    pushToast(
      `${delta > 0 ? 'Priority raised' : 'Priority lowered'} — ${domain}: ${result.affected} row(s).`,
      'success',
    );
  }

  async function handleSaveSettings() {
    setConfirm(null);
    try {
      const saved = await saveFrontierSettings(settings, fetch, adminToken);
      setSettings(saved.settings);
      setSavedSettings(saved.settings);
      pushToast('Settings saved.', 'success');
    } catch {
      pushToast('Saving settings failed.', 'error');
    }
  }

  function handleRevertSettings() {
    setSettings(savedSettings);
  }

  const dirty = JSON.stringify(settings) !== JSON.stringify(savedSettings);

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
  const kafkaLag = typeof status?.kafka_lag_total === 'number' ? status?.kafka_lag_total : null;

  return (
    <GlassPanel variant="rail">
      <Drawer
        triggerLabel={open ? 'Hide frontier' : 'Show frontier'}
        open={open}
        onToggle={() => void handleToggle()}
        panelId="frontier-panel"
        panelTestId="frontier-panel"
        label="Frontier dashboard"
        closedSummary="Frontier hidden — reopen to review fleet status and queues."
      >
        {loading ? (
          <div aria-live="polite">
            <p>Loading frontier…</p>
            <Skeleton bars={4} />
          </div>
        ) : snapshotFailed || status === null ? (
          <div role="alert" data-testid="frontier-snapshot-error" className="my-ol-s text-ol-s">
            <p>Frontier status unavailable.</p>
            <Button variant="secondary" size="sm" onClick={() => void snapshot.refetch()}>
              Retry
            </Button>
          </div>
        ) : (
          <>
            <section className="ol-dash-group" aria-label="Fleet status">
              <h2 className="ol-dash-title">Fleet status</h2>
              <p data-testid="frontier-state" className="my-ol-s">
                {status.paused ? (
                  <StatusBadge tone="paused" icon="dot">
                    Paused
                  </StatusBadge>
                ) : (
                  <StatusBadge tone="running" icon="dot">
                    Running
                  </StatusBadge>
                )}
              </p>
              <p data-testid="frontier-pause-hint" className="my-ol-xs text-ol-s">
                {status.paused ? 'Paused — queue growth halted' : 'Running'}
              </p>

              <div className="flex flex-wrap gap-ol-s my-ol-s">
                <Button
                  variant="secondary"
                  onClick={() => void handlePause()}
                  disabled={status.paused}
                >
                  Pause
                </Button>{' '}
                <Button
                  variant="secondary"
                  onClick={() => void handleResume()}
                  disabled={!status.paused}
                >
                  Resume
                </Button>{' '}
                <Button variant="ghost" onClick={() => void refresh()}>
                  Refresh
                </Button>{' '}
                <Button variant="destructive" onClick={() => setConfirm({ kind: 'clear' })}>
                  Pause &amp; clear
                </Button>{' '}
                <span data-testid="frontier-live">
                  {live ? (
                    <StatusBadge tone="live" icon="dot">
                      Live
                    </StatusBadge>
                  ) : (
                    <StatusBadge tone="stale" icon="dot">
                      Snapshot
                    </StatusBadge>
                  )}
                </span>
              </div>

              <h3 className="mt-ol-m mb-ol-s text-ol-m">Checkpoint</h3>
              <p data-testid="frontier-checkpoint" className="my-ol-s">
                {checkpoint
                  ? `${checkpoint.phase} · ${checkpoint.pages_indexed}/${checkpoint.pages_attempted} pages`
                  : 'No checkpoint yet'}
              </p>

              <h3 className="mt-ol-m mb-ol-s text-ol-m">Crawl diagnostics</h3>
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

              <WorkersTable workers={workers} />
            </section>
            <details className="ol-disclosure" aria-label="Queue">
              <summary className="ol-disclosure-title">
                Queue ({status.queue_depth ?? 0}
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
            <details className="ol-disclosure" aria-label="New URLs">
              <summary className="ol-disclosure-title">New URLs ({newUrls.length})</summary>
              {newUrls.length === 0 ? (
                <p className="my-ol-s text-ol-s">No new URLs</p>
              ) : (
                <ul
                  data-testid="frontier-new-urls"
                  aria-label="New URLs"
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
            <DomainTables
              onDomainAction={(op, domains) => handleDomainAction(op, domains)}
              onBlockDomain={(domain) => handleBlockDomain(domain)}
              onAutoTriage={() => handleAutoTriage()}
              onQueuedPriority={(domain, delta) => handleQueuedPriority(domain, delta)}
              onMutated={() => {
                void snapshot.refetch();
              }}
            />
            <section className="ol-dash-group" aria-label="Crawl request review">
              <CrawlRequests
                adminToken={adminToken}
                onChanged={() => {
                  void snapshot.refetch();
                }}
              />
            </section>
            <section className="ol-dash-group" aria-label="Seeds">
              <h2 className="ol-dash-title">Seeds</h2>
              <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                Seed URLs (one per line)
                <textarea
                  data-testid="frontier-seeds"
                  value={seeds}
                  onChange={(event) => setSeeds(event.target.value)}
                  rows={3}
                  className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                />
              </label>
              <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                Priority for these seeds (higher dequeues first; blank keeps stored)
                <input
                  data-testid="frontier-priority"
                  type="number"
                  step={1}
                  min={0}
                  value={priority}
                  aria-invalid={priorityError !== null}
                  aria-describedby={priorityError ? 'frontier-priority-error' : undefined}
                  onChange={(event) => {
                    setPriority(event.target.value);
                    setPriorityError(null);
                  }}
                  placeholder="0"
                  className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                />
              </label>
              {priorityError ? (
                <FieldError id="frontier-priority-error" message={priorityError} />
              ) : null}
              <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                Depth for these seeds (link-following starts here; blank means 0)
                <input
                  data-testid="frontier-depth"
                  type="number"
                  step={1}
                  min={0}
                  value={depth}
                  aria-invalid={depthError !== null}
                  aria-describedby={depthError ? 'frontier-depth-error' : undefined}
                  onChange={(event) => {
                    setDepth(event.target.value);
                    setDepthError(null);
                  }}
                  placeholder="0"
                  className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                />
              </label>
              {depthError ? <FieldError id="frontier-depth-error" message={depthError} /> : null}
              <div className="mt-ol-s flex flex-wrap gap-ol-s">
                <Button
                  variant="secondary"
                  data-testid="frontier-seeds-submit"
                  onClick={() => void handleEnqueueSeeds()}
                >
                  Enqueue seeds
                </Button>
              </div>
            </section>
            <section className="ol-dash-group" aria-label="Inspect URL">
              <h2 className="ol-dash-title">Inspect</h2>
              <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
                URL to inspect
                <input
                  data-testid="frontier-inspect-url"
                  type="url"
                  value={inspectUrl}
                  onChange={(event) => setInspectUrl(event.target.value)}
                  placeholder="https://example.com/page"
                  className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                />
              </label>
              <div className="mt-ol-s">
                <Button
                  variant="secondary"
                  data-testid="frontier-inspect-submit"
                  disabled={inspectBusy || inspectUrl.trim() === ''}
                  onClick={() => void handleInspectUrl()}
                >
                  {inspectBusy ? 'Inspecting…' : 'Inspect URL'}
                </Button>
              </div>
              {inspectError ? (
                <p data-testid="frontier-inspect-error" role="alert" className="my-ol-s text-ol-s">
                  {inspectError}
                </p>
              ) : null}
              {inspectResult ? (
                <div data-testid="frontier-inspect-result" className="my-ol-s text-ol-s">
                  <h3 className="mt-ol-m mb-ol-s text-ol-m">Normalized</h3>
                  <p className="my-ol-xs break-all">{inspectResult.normalized_url}</p>
                  <h3 className="mt-ol-m mb-ol-s text-ol-m">Queue</h3>
                  {inspectResult.frontier ? (
                    <p data-testid="frontier-inspect-frontier" className="my-ol-xs">
                      <strong>Queue:</strong> {inspectResult.frontier.status}
                      {' · '}priority {inspectResult.frontier.priority ?? 0}
                      {' · '}attempts {inspectResult.frontier.attempts ?? 0}
                      {inspectResult.frontier.last_error
                        ? ` · ${inspectResult.frontier.last_error}`
                        : ''}
                    </p>
                  ) : (
                    <p data-testid="frontier-inspect-frontier" className="my-ol-xs">
                      Queue: never seen — enqueue it first.
                    </p>
                  )}
                  <h3 className="mt-ol-m mb-ol-s text-ol-m">Indexed</h3>
                  {inspectResult.document ? (
                    <p data-testid="frontier-inspect-document" className="my-ol-xs">
                      <strong>Indexed:</strong> {inspectResult.document.title}
                      {inspectResult.document.chars !== undefined
                        ? ` (${inspectResult.document.chars} chars)`
                        : ''}
                    </p>
                  ) : (
                    <p data-testid="frontier-inspect-document" className="my-ol-xs">
                      Indexed: no — not crawled yet or fetch failed.
                    </p>
                  )}
                  <h3 className="mt-ol-m mb-ol-s text-ol-m">Links</h3>
                  <p data-testid="frontier-inspect-links" className="my-ol-xs">
                    <strong>Extracted links:</strong> {inspectResult.extracted_links ?? 0}
                  </p>
                  {(inspectResult.link_sample ?? []).length > 0 ? (
                    <ul
                      aria-label="Extracted link sample"
                      className="flex flex-col gap-ol-xs my-ol-xs pl-ol-m"
                    >
                      {(inspectResult.link_sample ?? []).map((target) => (
                        <li key={target} className="break-all">
                          {target}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              ) : null}
            </section>
            <section className="ol-dash-group" aria-label="Frontier settings">
              <h2 className="ol-dash-title">
                Settings {dirty ? <Badge>Unsaved changes</Badge> : null}
              </h2>
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
                    className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
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
                    className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
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
                    className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
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
                    className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
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
                    className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
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
                    className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
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
                    className="font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
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
                      className="flex-1 font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                    />
                    <Button variant="secondary" onClick={handleAddSkipDomain}>
                      Add
                    </Button>
                  </div>
                  {(settings.skip_domains ?? []).length > 0 && (
                    <ul
                      data-testid="frontier-skip-list"
                      className="flex flex-wrap gap-ol-xs ol-domain-box-list"
                    >
                      {(settings.skip_domains ?? []).map((domain) => (
                        <li key={domain}>
                          <Badge>{domain}</Badge>{' '}
                          <Button
                            variant="ghost"
                            data-testid={`frontier-skip-remove-${domain}`}
                            aria-label={`Remove ${domain} from skip domains`}
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
                      className="flex-1 font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                    />
                    <Button variant="secondary" onClick={handleAddAllowDomain}>
                      Add
                    </Button>
                  </div>
                  {(settings.allow_domains ?? []).length > 0 && (
                    <ul
                      data-testid="frontier-allow-list"
                      className="flex flex-wrap gap-ol-xs ol-domain-box-list"
                    >
                      {(settings.allow_domains ?? []).map((domain) => (
                        <li key={domain}>
                          <Badge>{domain}</Badge>{' '}
                          <Button
                            variant="ghost"
                            data-testid={`frontier-allow-remove-${domain}`}
                            aria-label={`Remove ${domain} from allow domains`}
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
                      className="flex-1 font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                    />
                    <Button variant="secondary" onClick={handleAddDenyDomain}>
                      Add
                    </Button>
                  </div>
                  {(settings.deny_domains ?? []).length > 0 && (
                    <ul
                      data-testid="frontier-deny-list"
                      className="flex flex-wrap gap-ol-xs ol-domain-box-list"
                    >
                      {(settings.deny_domains ?? []).map((domain) => (
                        <li key={domain}>
                          <Badge>{domain}</Badge>{' '}
                          <Button
                            variant="ghost"
                            data-testid={`frontier-deny-remove-${domain}`}
                            aria-label={`Remove ${domain} from deny domains`}
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
                      className="flex-1 font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
                    />
                    <Button variant="secondary" onClick={handleAddFeed}>
                      Add
                    </Button>
                  </div>
                  {(settings.discovery_feeds ?? []).length > 0 && (
                    <ul
                      data-testid="frontier-feed-list"
                      className="flex flex-col gap-ol-xs ol-domain-box-list"
                    >
                      {(settings.discovery_feeds ?? []).map((feed) => (
                        <li key={feed} className="flex items-center gap-ol-xs">
                          <Badge>{feed}</Badge>{' '}
                          <Button
                            variant="ghost"
                            data-testid={`frontier-feed-remove-${feed}`}
                            aria-label={`Remove ${feed} from discovery feeds`}
                            onClick={() => handleRemoveFeed(feed)}
                          >
                            ×
                          </Button>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                <div className="mt-ol-s flex flex-wrap items-center gap-ol-s ol-sticky-footer">
                  <Button
                    variant="primary"
                    data-testid="frontier-settings-save"
                    onClick={() => setConfirm({ kind: 'save' })}
                  >
                    Save settings
                  </Button>
                  <Button
                    variant="secondary"
                    data-testid="frontier-settings-revert"
                    disabled={!dirty}
                    onClick={() => void handleRevertSettings()}
                  >
                    Revert
                  </Button>
                  {dirty ? <span className="text-ol-s">Unsaved changes</span> : null}
                </div>
              </Card>
            </section>
          </>
        )}
      </Drawer>
      <ConfirmDialog
        open={confirm?.kind === 'clear'}
        title="Pause crawling and clear the queue?"
        body="Permanently drops all queued URLs. In-flight and history are kept."
        confirmLabel="Pause & clear"
        tone="destructive"
        onConfirm={() => void handleClear()}
        onCancel={() => setConfirm(null)}
      />
      <ConfirmDialog
        open={confirm?.kind === 'save'}
        title="Push settings to fleet?"
        body="The whole settings document is replaced; workers pick it up on refresh."
        confirmLabel="Save settings"
        tone="default"
        onConfirm={() => void handleSaveSettings()}
        onCancel={() => setConfirm(null)}
      />
      <Toasts items={toasts} onDismiss={dismissToast} />
    </GlassPanel>
  );
}

/**
 * Self-contained Query provider so the dashboard works wherever it is
 * mounted (admin page, tests) with the shared admin client config.
 */
export function FrontierDrawer({ adminToken }: FrontierDrawerProps) {
  const [client] = useState(createAdminQueryClient);
  return (
    <QueryClientProvider client={client}>
      <FrontierDrawerView adminToken={adminToken} />
    </QueryClientProvider>
  );
}
