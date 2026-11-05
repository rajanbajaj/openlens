import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState } from 'react';
import type { ReactNode } from 'react';

/**
 * Single shared Query client configuration for the admin app
 * (`retry: false`, no refocus refetch). Previously `FrontierDrawer` and
 * `AnalyticsPanel` each built an identical client inline; both now use
 * this factory (and `main.tsx` provides it app-wide) so the config
 * lives in exactly one place.
 */
export function createAdminQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
    },
  });
}

/** App-wide Query provider using the shared admin client config. */
export function AdminQueryProvider({ children }: { children: ReactNode }) {
  const [client] = useState(createAdminQueryClient);
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
