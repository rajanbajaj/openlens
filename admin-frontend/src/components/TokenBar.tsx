import { useState } from 'react';
import { Button } from './ui/Button.tsx';

/**
 * Admin token bar: password input kept in tab-scoped sessionStorage.
 * sessionStorage (not localStorage) bounds a stolen-token window to the
 * open tab: any XSS would still read memory, but the secret no longer
 * survives tab close on shared machines. The Forget button clears both.
 */
export interface TokenBarProps {
  adminToken: string;
  onChange: (token: string) => void;
}

const STORAGE_KEY = 'openlens-admin-token';

/** Read the persisted token (empty when absent or unreadable). */
export function loadStoredToken(storage: Pick<Storage, 'getItem'> = sessionStorage): string {
  try {
    return storage.getItem(STORAGE_KEY) ?? '';
  } catch {
    return '';
  }
}

/** Persist the token; storage failures are ignored (memory still holds it). */
export function storeToken(
  token: string,
  storage: Pick<Storage, 'setItem' | 'removeItem'> = sessionStorage,
): void {
  try {
    if (token) {
      storage.setItem(STORAGE_KEY, token);
    } else {
      storage.removeItem(STORAGE_KEY);
    }
  } catch {
    /* private-mode or unavailable storage: session-only token */
  }
}

export function TokenBar({ adminToken, onChange }: TokenBarProps) {
  const [visible, setVisible] = useState(false);

  return (
    <div className="flex flex-col gap-ol-xs">
      <label
        htmlFor="frontier-admin-token"
        className="flex flex-col gap-ol-xs text-ol-s font-semibold"
      >
        Admin token
      </label>
      <p className="text-ol-s text-ol-ink-soft">
        (required only when the backend sets OPENLENS_FRONTIER_ADMIN_TOKEN)
      </p>
      <span className="flex items-center gap-ol-xs">
        <input
          id="frontier-admin-token"
          data-testid="frontier-admin-token"
          type={visible ? 'text' : 'password'}
          autoComplete="off"
          value={adminToken}
          onChange={(event) => {
            onChange(event.target.value);
            storeToken(event.target.value);
          }}
          className="flex-1 font-normal text-ol-ink bg-white border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
        />
        <Button
          variant="ghost"
          size="sm"
          data-testid="frontier-admin-token-visibility"
          aria-pressed={visible}
          onClick={() => setVisible((current) => !current)}
        >
          {visible ? 'Hide token' : 'Show token'}
        </Button>
      </span>
      {adminToken ? (
        <span>
          <Button
            variant="ghost"
            size="sm"
            data-testid="frontier-admin-forget"
            onClick={() => {
              onChange('');
              storeToken('');
            }}
          >
            Forget token
          </Button>
        </span>
      ) : null}
    </div>
  );
}
