import { useRef, useState } from 'react';
import type { AdminConfigName } from './api/types.ts';
import { CRAWLER_DEFAULTS } from './crawlerDefaults.ts';
import { AnalyticsPanel } from './components/AnalyticsPanel.tsx';
import { ConfigEditor } from './components/ConfigEditor.tsx';
import { FrontierDrawer } from './components/FrontierDrawer.tsx';
import { Logo } from './components/Logo.tsx';
import { TokenBar, loadStoredToken } from './components/TokenBar.tsx';
import { Tour } from './components/Tour.tsx';
import { GlassPanel } from './components/ui/GlassPanel.tsx';
import { TOUR_STEPS } from './tourSteps.ts';

type AdminTab = 'frontier' | 'analytics' | AdminConfigName;

const TABS: Array<{ id: AdminTab; label: string }> = [
  { id: 'frontier', label: 'URL frontier' },
  { id: 'analytics', label: 'Analytics' },
  { id: 'models', label: 'Models' },
  { id: 'providers', label: 'Providers' },
  { id: 'ranking', label: 'Ranking' },
  { id: 'query', label: 'Query' },
  { id: 'plugins', label: 'Plugins' },
  { id: 'indexing', label: 'Indexing' },
];

/**
 * File-seed defaults mirroring the shipped YAML configs. Shown in the
 * editors while the backend `/api/v1/admin/config/*` routes are still
 * pending; replaced by the effective document once they land.
 */
const FILE_DEFAULTS: Record<AdminConfigName, Record<string, unknown>> = {
  models: {
    primary: 'fake',
    fake: { enabled: false, type: 'fake', timeout_s: 10 },
  },
  providers: {
    strategy: 'single',
    primary: 'custom_index',
    custom_index: { enabled: true, type: 'custom_index', timeout_s: 5 },
  },
  ranking: {
    default_profile: 'default',
    profiles: {
      default: { description: 'Baseline.', relevance_weight: 1.0, warning_penalty: 0.5 },
    },
  },
  query: {
    normalizer: { lowercase: true, max_length: 1000 },
  },
  plugins: {},
  indexing: {
    timeout_s: 5,
    crawl_delay_s: 0.5,
    max_pages: 100,
    max_queued_urls: CRAWLER_DEFAULTS.max_queued_urls,
    respect_robots: true,
    default_max_depth: CRAWLER_DEFAULTS.default_max_depth,
    deny_url_patterns: [],
  },
};

const TAB_DESCRIPTIONS: Record<AdminConfigName, string> = {
  models: 'Model backends: only enabled entries boot. All ship disabled (offline-safe).',
  providers:
    'Search providers: strategy, primary, and per-entry enable flags. Only custom_index ships.',
  ranking:
    'Ranking profiles keyed by search mode. Unknown keys and non-finite weights are rejected.',
  query: 'Query pipeline: normalizer, intent, expansion, and context sections.',
  plugins: 'Plugin chain configuration (per-plugin validation applies on save).',
  indexing:
    'Crawler: timeouts, politeness, page caps, and default_max_depth (link-following depth; seeds are depth 0). Takes effect on worker restart.',
};

/**
 * OpenLens admin shell: token login bar plus the URL-frontier
 * dashboard and the models/providers/ranking/query/plugins/indexing editors.
 * Every mutation reuses the backend `X-Admin-Token` gate; the user
 * search UI keeps no admin affordances.
 */
export default function App() {
  const [adminToken, setAdminToken] = useState(() => loadStoredToken());
  const [tab, setTab] = useState<AdminTab>('frontier');
  const [tourRun, setTourRun] = useState(0);
  const tabsRef = useRef<HTMLElement>(null);

  /** Tablist keyboard support: arrows move + activate, Home/End jump. */
  function handleTabsKeyDown(event: React.KeyboardEvent): void {
    const order = TABS.map((entry) => entry.id);
    const current = order.indexOf(tab);
    let next: number | null = null;
    if (event.key === 'ArrowRight') next = (current + 1) % order.length;
    else if (event.key === 'ArrowLeft') next = (current - 1 + order.length) % order.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = order.length - 1;
    if (next === null) return;
    event.preventDefault();
    const id = order[next];
    setTab(id);
    tabsRef.current?.querySelector<HTMLButtonElement>(`[data-testid="admin-tab-${id}"]`)?.focus();
  }

  return (
    <main className="ol-admin">
      <GlassPanel variant="hero" className="ol-admin-hero">
        <h1 className="ol-brand-heading">
          <Logo size="header" />
          <span className="ol-admin-tag">Admin</span>
        </h1>
        <div className="ol-admin-hero-controls">
          <TokenBar adminToken={adminToken} onChange={setAdminToken} />
          <button
            type="button"
            className="ol-button ol-button-secondary"
            onClick={() => {
              setTourRun((n) => n + 1);
            }}
          >
            Take a tour
          </button>
        </div>
      </GlassPanel>
      <Tour steps={TOUR_STEPS} runId={tourRun} storageKey="ol-tour-admin" />
      <nav
        aria-label="Admin sections"
        role="tablist"
        className="ol-admin-tabs"
        ref={tabsRef}
        onKeyDown={handleTabsKeyDown}
      >
        {TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            role="tab"
            tabIndex={tab === entry.id ? 0 : -1}
            data-testid={`admin-tab-${entry.id}`}
            aria-selected={tab === entry.id}
            onClick={() => setTab(entry.id)}
            className="ol-admin-tab"
          >
            {entry.label}
          </button>
        ))}
      </nav>
      {tab === 'frontier' ? (
        <FrontierDrawer adminToken={adminToken} />
      ) : tab === 'analytics' ? (
        <AnalyticsPanel adminToken={adminToken} />
      ) : (
        <ConfigEditor
          name={tab}
          title={TABS.find((entry) => entry.id === tab)?.label ?? tab}
          description={TAB_DESCRIPTIONS[tab]}
          adminToken={adminToken}
          fileDefault={FILE_DEFAULTS[tab]}
        />
      )}
    </main>
  );
}
