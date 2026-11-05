import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import axe from 'axe-core';
import App from '../../App.tsx';
import { ConfirmDialog } from './ConfirmDialog.tsx';
import { DataTable } from './DataTable.tsx';
import { EmptyState } from './EmptyState.tsx';
import { FieldError } from './FieldError.tsx';
import { SearchInput } from './SearchInput.tsx';
import { StatusBadge } from './StatusBadge.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/**
 * axe smoke assertions for the shell and every new primitive.
 * Color-contrast is disabled: jsdom has no layout, so every contrast
 * check is a false positive there; contrast pairs ship from the
 * existing `--color-ol-*` AA tokens instead.
 */
async function expectNoViolations(container: HTMLElement): Promise<void> {
  const results = await axe.run(container, {
    rules: { 'color-contrast': { enabled: false } },
  });
  expect(results.violations).toEqual([]);
}

describe('accessibility', () => {
  it('admin shell has no violations', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('backend down')));
    const { container } = render(<App />);
    await expectNoViolations(container);
  });

  it('new primitives have no violations', async () => {
    const { container } = render(
      <div>
        <DataTable
          rows={[{ id: 'a', url: 'https://example.com' }]}
          columns={[
            {
              key: 'url',
              header: 'URL',
              sortable: true,
              sortValue: (row) => row.url,
              render: (row) => <span>{row.url}</span>,
            },
          ]}
          rowKey={(row) => row.id}
          pagination={{ page: 0, pageSize: 10, onPage: () => {} }}
          ariaLabel="Example rows"
        />
        <SearchInput
          value=""
          onChange={() => {}}
          placeholder="Search…"
          ariaLabel="Search rows"
          debounceMs={0}
        />
        <ConfirmDialog
          open
          title="Confirm title"
          body="Confirm body."
          confirmLabel="Confirm"
          tone="default"
          onConfirm={() => {}}
          onCancel={() => {}}
        />
        <StatusBadge tone="running" icon="dot">
          Running
        </StatusBadge>
        <EmptyState title="Nothing here" hint="Try again later." />
        <FieldError id="example-error" message="Something is wrong." />
      </div>,
    );
    await expectNoViolations(container);
  });
});
