import type { FrontierSettings } from './api/types.ts';

/**
 * Shared numeric fallback for crawler queue caps, imported by both the
 * admin shell file-default preview (`App.tsx`) and the frontier
 * dashboard pre-load defaults (`FrontierDrawer.tsx`).
 *
 * Values mirror the backend enforcement contract
 * (`FrontierSettingsSchema` in `src/openlens/api/schemas.py`:
 * `max_queued_urls` default 1000, `default_max_depth` default 2), which
 * is what the settings PUT validates against. Note the shipped
 * `configs/default.yaml` indexing seed differs (`max_queued_urls: 10000`);
 * the backend coerces and validates on save, so both surfaces share
 * the enforced numbers until the routes converge.
 */
export const CRAWLER_DEFAULTS: Pick<FrontierSettings, 'max_queued_urls' | 'default_max_depth'> = {
  max_queued_urls: 1000,
  default_max_depth: 2,
};
