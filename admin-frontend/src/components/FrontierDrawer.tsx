import { useEffect, useState } from 'react';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import {
  clearFrontier,
  frontierLiveUrl,
  pauseFrontier,
  resumeFrontier,
  saveFrontierSettings,
} from '../api/client.ts';
import {
  frontierGetSettingsApiV1FrontierSettingsGet,
  frontierListWorkersApiV1FrontierWorkersGet,
  frontierStatusApiV1FrontierStatusGet,
} from '../api/generated/openlens.ts';
import {
  isFrontierStatus,
  readFrontierNewUrls,
  readFrontierQueue,
  readFrontierSettings,
  readFrontierWorkers,
} from '../api/guard.ts';
import type { FrontierSettings, FrontierStatus, FrontierWorker } from '../api/types.ts';
import { Badge } from './ui/Badge.tsx';
import { Button } from './ui/Button.tsx';
import { Card } from './ui/Card.tsx';
import { Drawer } from './ui/Drawer.tsx';
import { GlassPanel } from './ui/GlassPanel.tsx';
import { Toasts, useToasts } from './ui/Toast.tsx';

/** Local defaults used before the backend settings load. */
const DEFAULT_SETTINGS: FrontierSettings = {
  max_pages: 100,
  max_queued_urls: 1000,
  crawl_delay_s: 0.5,
  default_max_depth: 3,
  respect_robots: true,
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
        if (isFrontierStatus(data)) setStatus(data);
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

  async function handleSaveSettings() {
    try {
      const saved = await saveFrontierSettings(settings, fetch, adminToken);
      setSettings(saved.settings);
      pushToast('Settings saved.', 'success');
    } catch {
      pushToast('Saving settings failed.', 'error');
    }
  }

  const queue = status ? readFrontierQueue(status) : [];
  const newUrls = status ? readFrontierNewUrls(status) : [];
  const checkpoint = status?.checkpoint;

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

            <h4 className="mt-ol-m mb-ol-s text-ol-m">Workers ({workers.length})</h4>
            {workers.length === 0 ? (
              <p className="my-ol-s text-ol-s">No workers heartbeating</p>
            ) : (
              <ul
                data-testid="frontier-workers"
                className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
              >
                {workers.map((worker) => (
                  <li key={worker.worker_id}>
                    <Badge>{worker.status}</Badge> {worker.worker_id} · {worker.claimed} claimed ·
                    crawl {worker.crawl_id}
                  </li>
                ))}
              </ul>
            )}

            <h4 className="mt-ol-m mb-ol-s text-ol-m">Queue ({status?.queue_depth ?? 0})</h4>
            {queue.length === 0 ? (
              <p className="my-ol-s text-ol-s">Queue is empty</p>
            ) : (
              <ol
                data-testid="frontier-queue"
                aria-label="Frontier queue"
                className="flex min-h-0 flex-col gap-ol-xs my-ol-s pl-ol-m max-h-64 overflow-y-auto"
              >
                {queue.map((entry) => (
                  <li key={entry.url}>
                    <Badge>{entry.status}</Badge> {entry.url} · depth {entry.depth} · priority{' '}
                    {entry.priority ?? 0}
                  </li>
                ))}
              </ol>
            )}

            <h4 className="mt-ol-m mb-ol-s text-ol-m">New URLs ({newUrls.length})</h4>
            {newUrls.length === 0 ? (
              <p className="my-ol-s text-ol-s">No new URLs</p>
            ) : (
              <ul
                data-testid="frontier-new-urls"
                className="flex flex-col gap-ol-xs my-ol-s pl-ol-m"
              >
                {newUrls.map((entry) => (
                  <li key={entry.url}>
                    <Badge>{entry.status}</Badge> {entry.url}
                  </li>
                ))}
              </ul>
            )}

            <h4 className="mt-ol-m mb-ol-s text-ol-m">Enqueue seeds</h4>
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

            <h4 className="mt-ol-m mb-ol-s text-ol-m">Settings</h4>
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
              <div className="mt-ol-s">
                <Button variant="secondary" onClick={() => void handleSaveSettings()}>
                  Save settings
                </Button>
              </div>
            </Card>
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
