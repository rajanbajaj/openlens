/** Admin token bar: password input persisted to localStorage. */
export interface TokenBarProps {
  adminToken: string;
  onChange: (token: string) => void;
}

const STORAGE_KEY = 'openlens-admin-token';

/** Read the persisted token (empty when absent or unreadable). */
export function loadStoredToken(storage: Pick<Storage, 'getItem'> = localStorage): string {
  try {
    return storage.getItem(STORAGE_KEY) ?? '';
  } catch {
    return '';
  }
}

/** Persist the token; storage failures are ignored (memory still holds it). */
export function storeToken(token: string, storage: Pick<Storage, 'setItem'> = localStorage): void {
  try {
    storage.setItem(STORAGE_KEY, token);
  } catch {
    /* private-mode or unavailable storage: session-only token */
  }
}

export function TokenBar({ adminToken, onChange }: TokenBarProps) {
  return (
    <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
      Admin token (required only when the backend sets OPENLENS_FRONTIER_ADMIN_TOKEN)
      <input
        data-testid="frontier-admin-token"
        type="password"
        autoComplete="off"
        value={adminToken}
        onChange={(event) => {
          onChange(event.target.value);
          storeToken(event.target.value);
        }}
        className="font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
      />
    </label>
  );
}
