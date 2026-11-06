# Troubleshoot: admin `timeout_s` edit changed nothing (still 5s)

## Symptoms (found 2026-11-06)

- Admin → Providers → `custom_index.timeout_s` set to `10`, saved
  (persisted to the config store, visible on re-read).
- Next `love` query still 502s; the body still says
  `timed out after 5.0s`. A backend restart did not help either.

## Root cause

`CustomIndexProvider` extended the provider ABC directly instead of
`BaseSearchProvider`, so its `configure()` never read `timeout_s` and
the manager's live `getattr(provider, "timeout_s", 5.0)` fell back to
the default forever. The 502 body names the enforced budget — trust it
over the admin screen. The saved value was loading correctly at boot;
the running object just never consumed it.

## Fix

`src/openlens/providers/custom_index.py` now extends
`BaseSearchProvider` (`super().__init__()` + `super().configure()`),
so admin/boot `timeout_s` reaches the live object. Regression tests:
`test_configure_applies_timeout_s`,
`test_configure_rejects_bad_timeout_s`.

## Standing rule

Admin → Config writes persist + overlay in-memory reads, but live
provider objects are built at boot. **Any provider change
(`timeout_s`, enable/disable) needs a backend restart:**

```bash
docker restart openlens-local-backend-1
```

then re-run the query and confirm the 502 body (if any) names the new
budget. Release pending for this fix.
