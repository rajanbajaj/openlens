import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { buildFilters, EMPTY_FILTER_STATE, type UserFilterState } from '../api/filters.ts';
import type { ProviderPrefs } from '../api/types.ts';
import { Sidebar } from './Sidebar.tsx';

afterEach(() => {
  cleanup();
});

const EMPTY_PREFS: ProviderPrefs = { include: [], exclude: [], primary: null };

function setup(
  overrides: {
    filters?: UserFilterState;
    prefs?: ProviderPrefs;
    knownProviders?: string[];
    appliedFilters?: string[];
    disabled?: boolean;
    collapsed?: boolean;
  } = {},
) {
  const onFiltersChange = vi.fn();
  const onPrefsChange = vi.fn();
  const onCollapsedChange = vi.fn();
  const onChipRemove = vi.fn();
  render(
    <Sidebar
      filters={overrides.filters ?? EMPTY_FILTER_STATE}
      onFiltersChange={onFiltersChange}
      prefs={overrides.prefs ?? EMPTY_PREFS}
      onPrefsChange={onPrefsChange}
      knownProviders={overrides.knownProviders ?? ['mock']}
      appliedFilters={overrides.appliedFilters ?? []}
      disabled={overrides.disabled}
      collapsed={overrides.collapsed ?? false}
      onCollapsedChange={onCollapsedChange}
      onChipRemove={onChipRemove}
    />,
  );
  return { onFiltersChange, onPrefsChange, onCollapsedChange, onChipRemove };
}

describe('Sidebar', () => {
  it('renders every filter surface standalone with mocked props', () => {
    setup({
      filters: { ...EMPTY_FILTER_STATE, maxPrice: '250', excludeSponsored: true },
      prefs: { include: [], exclude: [], primary: 'mock' },
      appliedFilters: ['user-restrictions'],
    });

    expect(screen.getByTestId('sidebar-panel')).toHaveClass('ol-glass-sidebar');
    expect(screen.getByRole('button', { name: 'Hide filters' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    // Sponsored toggle + all seven panel fields (exact labels so the
    // chip remove buttons, e.g. "Remove Max price 250", do not match).
    expect(screen.getByLabelText(/exclude sponsored/i)).toBeChecked();
    for (const label of [
      'Max price',
      'Location',
      'Facilities',
      'Prefer (preference keyword)',
      'Blocked domain',
      'Allowed domain',
      'Exclude term',
    ]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    }
    // Provider prefs + backend attribution + user chips.
    expect(screen.getByLabelText(/only these providers/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/exclude these providers/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/primary provider/i)).toHaveValue('mock');
    expect(screen.getByTestId('applied-filters')).toHaveTextContent(
      'Applied filters: user-restrictions',
    );
    expect(screen.getByTestId('user-filter-chips')).toHaveTextContent('Max price 250');
    expect(screen.getByTestId('user-filter-chips')).toHaveTextContent('Excludes sponsored');
  });

  it('renders the empty attribution text and no chips when nothing is set', () => {
    setup();
    expect(screen.getByTestId('applied-filters')).toHaveTextContent('No filters applied');
    expect(screen.queryByTestId('user-filter-chips')).not.toBeInTheDocument();
  });

  it('collapses to a lone toggle button and reports toggle clicks', () => {
    const { onCollapsedChange } = setup({ collapsed: true });

    const toggle = screen.getByRole('button', { name: 'Show filters' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveAttribute('aria-controls', 'ol-sidebar-panel');
    const panel = screen.getByTestId('sidebar-panel');
    expect(panel).not.toBeVisible();
    expect(screen.getByLabelText(/max price/i)).not.toBeVisible();

    fireEvent.click(toggle);
    expect(onCollapsedChange).toHaveBeenCalledWith(false);
  });

  it('expands the panel and reports collapse clicks', () => {
    const { onCollapsedChange } = setup({ collapsed: false });
    expect(screen.getByTestId('sidebar-panel')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: 'Hide filters' }));
    expect(onCollapsedChange).toHaveBeenCalledWith(true);
  });

  it('chip removal clears one field and preserves the rest', () => {
    const filters: UserFilterState = {
      ...EMPTY_FILTER_STATE,
      maxPrice: '250',
      location: 'Berlin',
    };
    const { onFiltersChange, onChipRemove } = setup({ filters });

    fireEvent.click(screen.getByRole('button', { name: 'Remove Max price 250' }));
    const cleared = { ...filters, maxPrice: '' };
    expect(onFiltersChange).toHaveBeenCalledWith(cleared);
    expect(onChipRemove).toHaveBeenCalledWith(cleared);
  });

  it('filter and prefs edits flow through the change callbacks', () => {
    const { onFiltersChange, onPrefsChange } = setup();

    fireEvent.change(screen.getByLabelText(/max price/i), { target: { value: '99' } });
    expect(onFiltersChange).toHaveBeenCalledWith({ ...EMPTY_FILTER_STATE, maxPrice: '99' });

    fireEvent.click(screen.getByLabelText(/exclude sponsored/i));
    expect(onFiltersChange).toHaveBeenCalledWith({ ...EMPTY_FILTER_STATE, excludeSponsored: true });

    fireEvent.change(screen.getByLabelText(/primary provider/i), { target: { value: 'mock' } });
    expect(onPrefsChange).toHaveBeenCalledWith({ include: [], exclude: [], primary: 'mock' });
  });

  it('emits byte-identical wire keys via buildFilters', () => {
    const full: UserFilterState = {
      excludeSponsored: true,
      maxPrice: '250',
      location: 'Berlin',
      facilities: 'wifi, pool',
      preferSuffix: 'Trust!',
      blockedDomain: 'example.com',
      allowedDomain: 'trusted.example',
      mustNotTerm: 'advertisement',
    };
    expect(JSON.stringify(buildFilters(full))).toBe(
      '{"must_not_terms":["sponsored","advertisement"],' +
        '"max_price":250,' +
        '"location":"Berlin",' +
        '"facilities":"wifi, pool",' +
        '"prefer_trust":true,' +
        '"blocked_domains":["example.com"],' +
        '"allowed_domains":["trusted.example"]}',
    );
    // Blank panel state sends nothing; the toggle is a UI concept only.
    expect(JSON.stringify(buildFilters(EMPTY_FILTER_STATE))).toBe('{}');
    expect(JSON.stringify(buildFilters(full))).not.toContain('exclude_sponsored');
  });

  it('UI callback payloads shape into the expected wire bytes', () => {
    const { onFiltersChange } = setup();
    fireEvent.click(screen.getByLabelText(/exclude sponsored/i));
    const payload = onFiltersChange.mock.calls[0][0] as UserFilterState;
    expect(JSON.stringify(buildFilters(payload))).toBe('{"must_not_terms":["sponsored"]}');
  });
});
