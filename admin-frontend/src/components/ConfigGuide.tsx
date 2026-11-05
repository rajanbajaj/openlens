import type { AdminConfigName } from '../api/types.ts';

/** One documented key inside a config doc. */
export interface ConfigFieldHelp {
  key: string;
  help: string;
}

/** Helper doc for one config section: purpose plus its load-bearing keys. */
export interface ConfigGuide {
  summary: string;
  fields: ConfigFieldHelp[];
}

export const CONFIG_GUIDES: Record<AdminConfigName, ConfigGuide> = {
  models: {
    summary:
      'AI backends (agent chat, semantic signals). Only entries with enabled: true boot; everything ships disabled so the stack stays offline-safe. Secrets come from environment, never from this doc.',
    fields: [
      { key: 'primary', help: 'Backend answering AI calls when several are enabled.' },
      {
        key: 'enabled',
        help: 'Per-backend switch. Key alone is not enough: MODEL_ENABLED=true plus a valid MODEL_API_KEY is required for the Meta backend.',
      },
      { key: 'timeout_s', help: 'Per-call bound for the model backend.' },
    ],
  },
  providers: {
    summary:
      'Search backends. custom_index (the Postgres-backed own crawl index) is the single source of truth; fallback only retries a second provider on primary failure, never on empty hits.',
    fields: [
      {
        key: 'strategy',
        help: "'single' serves primary only; 'fallback' retries the fallback provider once on primary failure.",
      },
      { key: 'primary', help: 'Provider serving every request. Must be enabled.' },
      {
        key: 'fallback',
        help: 'Optional second enabled provider (must differ from primary). Required when strategy is fallback.',
      },
      {
        key: 'timeout_s',
        help: 'Per-provider bound; a breach fails over (fallback strategy) instead of hanging search.',
      },
      {
        key: 'semantic_enabled',
        help: 'Fuse embedding signals via RRF. Needs an enabled models backend; any embedding error fails open to plain FTS.',
      },
    ],
  },
  ranking: {
    summary:
      'Named per-mode ranking profiles. default_profile selects the fallback; each mode tab (default, news, shopping, travel, child-safe) reads its own profile.',
    fields: [
      {
        key: 'default_profile',
        help: 'Profile used when a mode has no dedicated entry. Must name a known profile.',
      },
      {
        key: 'profiles',
        help: 'Map of profile name to weight bundle. Unknown keys and non-finite weights are rejected on save.',
      },
    ],
  },
  query: {
    summary:
      'Pre-retrieval pipeline (normalize → intent → expand), not a plugin. Context history is request-scoped and never persisted; child-safe is never suggested, only explicit.',
    fields: [
      {
        key: 'normalizer.max_length / on_overflow',
        help: 'Input bound (default 1000). Overflow truncates or rejects the query.',
      },
      {
        key: 'intent.keywords / tie_priority',
        help: 'Keyword sets mapping queries to modes (navigational, shopping, travel, news, informational) plus tie order.',
      },
      {
        key: 'expansion.synonym_sets / max_added_terms',
        help: 'OR-of-ANDs synonyms (default cap 8 added terms) so expansion recalls without AND-restricting.',
      },
      {
        key: 'expansion.expand_rewrite',
        help: 'Off by default. When true, also offers one single-substitution query-string variant.',
      },
      {
        key: 'context.enabled / max_history_terms',
        help: 'Request-scoped history recall. Off by default; enabling never stores history server-side.',
      },
    ],
  },
  plugins: {
    summary:
      'Post-retrieval pipeline plugins (list of {name, enabled, config}). The three filtering plugins always run first in registry order: user-restrictions → safety-filter → privacy-filter, regardless of list order.',
    fields: [
      {
        key: 'name / enabled',
        help: 'Registry name plus per-plugin switch. Unknown names are rejected on save.',
      },
      {
        key: 'config',
        help: 'Per-plugin knobs validated by that plugin (e.g. trust-scorer reputation lists, freshness half-life).',
      },
      {
        key: 'ai-summary',
        help: 'Ships disabled. Enabling also needs an enabled models backend; without one the engine omits it rather than falling back.',
      },
    ],
  },
  indexing: {
    summary:
      'Polite crawler + incremental indexer bounds. Politeness is max(crawl_delay_s, per-domain rate_limit_s, robots Crawl-delay). Edits apply to workers on refresh/restart.',
    fields: [
      {
        key: 'timeout_s',
        help: 'Per-fetch bound; hung hosts fail the URL instead of stalling the crawl.',
      },
      { key: 'crawl_delay_s', help: 'Global minimum gap between hits to the same host.' },
      {
        key: 'max_pages / max_bytes',
        help: 'Per-run fetch cap and per-response body cap (over-cap pages abort as max_bytes abort).',
      },
      {
        key: 'allow_domains / deny_domains / deny_url_patterns',
        help: 'Host gates. deny wins; patterns are case-insensitive substring hits on discovered URLs.',
      },
      {
        key: 'domain_policies.{allow, rate_limit_s, max_depth, max_pages}',
        help: 'Per-domain overrides: block/exempt a host, space out hits, cap link-following depth, or cap fetches (trap defense for near-empty hosts).',
      },
      {
        key: 'default_max_depth / respect_robots / user_agent',
        help: 'Link-following cap for the rest, robots.txt honoring (Disallow + Crawl-delay), and the bot identity sent to hosts.',
      },
      {
        key: 'max_attempts_per_url / checkpoint_interval_pages',
        help: 'Fetch retries (429/5xx/timeouts only; 4xx fail terminally) and checkpoint cadence.',
      },
    ],
  },
};

/**
 * Collapsible field guide for one config doc. Static helper content —
 * it never reads the edited text, so filtering or invalid JSON cannot
 * hide it.
 */
export function ConfigGuide({ name }: { name: AdminConfigName }) {
  const guide = CONFIG_GUIDES[name];
  return (
    <details className="my-ol-s" data-testid={`config-guide-${name}`}>
      <summary className="cursor-pointer font-semibold text-ol-s">Field guide: {name}</summary>
      <p className="mt-ol-xs text-ol-s text-ol-ink-soft">{guide.summary}</p>
      <ul className="mt-ol-xs flex flex-col gap-ol-xs text-ol-s">
        {guide.fields.map((field) => (
          <li key={field.key}>
            <code className="font-mono">{field.key}</code>
            <span> — {field.help}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}
