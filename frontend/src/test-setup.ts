import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';

// Exercise the suggestion machinery in tests while the demo keeps it
// off by default (see SUGGEST_ENABLED in SearchForm.tsx).
vi.stubEnv('VITE_ENABLE_SUGGEST', 'true');
