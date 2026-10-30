import { useState } from 'react';
import type { AdminConfigName } from './api/types.ts';
import { ConfigEditor } from './components/ConfigEditor.tsx';
import { FrontierDrawer } from './components/FrontierDrawer.tsx';
import { TokenBar, loadStoredToken } from './components/TokenBar.tsx';
import { GlassPanel } from './components/ui/GlassPanel.tsx';

type AdminTab = 'frontier' | AdminConfigName;

const TABS: Array<{ id: AdminTab; label: string }> = [
  { id: 'frontier', label: 'URL frontier' },
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
    max_queued_urls: 10000,
    respect_robots: true,
    default_max_depth: 2,
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

  return (
    <main className="ol-admin">
      <GlassPanel variant="hero">
        <h1>OpenLens Admin</h1>
        <TokenBar adminToken={adminToken} onChange={setAdminToken} />
      </GlassPanel>
      <nav aria-label="Admin sections" className="flex flex-wrap gap-ol-s my-ol-m">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            data-testid={`admin-tab-${entry.id}`}
            aria-pressed={tab === entry.id}
            onClick={() => setTab(entry.id)}
            className="px-ol-s py-ol-m rounded-ol-m border border-ol-glass-border"
          >
            {entry.label}
          </button>
        ))}
      </nav>
      {tab === 'frontier' ? (
        <FrontierDrawer adminToken={adminToken} />
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
