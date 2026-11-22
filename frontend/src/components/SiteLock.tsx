import { useState } from 'react';
import { Button } from './ui/Button.tsx';
import { GlassPanel } from './ui/GlassPanel.tsx';

/**
 * Password gate for env-locked deployments (`OPENLENS_SITE_PASSWORD`
 * set on the backend). Rendered instead of the whole search UI while
 * the site is locked; reports wrong passwords inline without
 * leaving the page.
 */
export interface SiteLockProps {
  /** Verify the password; resolves true when the site unlocks. */
  onUnlock: (password: string) => Promise<boolean>;
}

export default function SiteLock({ onUnlock }: SiteLockProps) {
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  async function submit() {
    if (!password || busy) return;
    setBusy(true);
    setFailed(false);
    try {
      const ok = await onUnlock(password);
      if (ok) {
        setPassword('');
      } else {
        setFailed(true);
      }
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Site login" data-testid="site-lock">
      <GlassPanel>
        <h1 data-testid="site-lock-title">This search is private</h1>
        <p>Enter the site password to continue.</p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <label htmlFor="site-password">Site password</label>
          <input
            id="site-password"
            data-testid="site-password"
            type="password"
            autoComplete="current-password"
            value={password}
            disabled={busy}
            onChange={(event) => {
              setPassword(event.target.value);
              setFailed(false);
            }}
          />
          {failed ? (
            <p role="alert" data-testid="site-password-error">
              Wrong password — try again.
            </p>
          ) : null}
          <Button
            variant="primary"
            type="submit"
            data-testid="site-unlock"
            disabled={busy || !password}
          >
            {busy ? 'Unlocking…' : 'Unlock search'}
          </Button>
        </form>
      </GlassPanel>
    </section>
  );
}
