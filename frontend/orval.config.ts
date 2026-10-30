import { defineConfig } from 'orval';

/**
 * Generate the typed API client from the backend OpenAPI snapshot.
 *
 * Regenerate after backend route/schema changes:
 *   1. Refresh the snapshot against the backend contract
 *      (see frontend/openapi/README.md).
 *   2. Run `npm run api:gen` and commit the output.
 *
 * The generated fetch client uses global `fetch`, so the existing
 * vitest fetch stubs keep working unchanged.
 */
export default defineConfig({
  openlens: {
    input: './openapi/openapi.json',
    output: {
      target: './src/api/generated/openlens.ts',
      schemas: './src/api/generated/schemas',
      client: 'fetch',
      clean: true,
      prettier: true,
      override: {
        mutator: undefined,
        query: {
          useQuery: true,
        },
      },
    },
  },
});
