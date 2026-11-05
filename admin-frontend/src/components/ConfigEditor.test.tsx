import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ConfigEditor } from './ConfigEditor.tsx';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const DOC = {
  doc: 'models',
  body: { primary: 'fake', extra: { nested: true } },
  version: 2,
  updated_at: null,
  source: 'db',
};

function stubGet(payload: unknown, status = 200): void {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(() =>
      Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        headers: new Headers(),
        text: () => Promise.resolve(JSON.stringify(payload)),
        json: () => Promise.resolve(payload),
      }),
    ),
  );
}

function renderEditor() {
  return render(
    <ConfigEditor
      name="models"
      title="Models"
      description="Model backends."
      adminToken=""
      fileDefault={{ primary: 'fake' }}
    />,
  );
}

describe('ConfigEditor', () => {
  it('disables the textarea with a skeleton until loaded', async () => {
    stubGet(DOC);
    renderEditor();
    const area = screen.getByTestId('config-editor-models-text') as HTMLTextAreaElement;
    expect(area.disabled).toBe(true);
    await waitFor(() => {
      expect(screen.getByTestId('config-editor-models-text')).not.toBeDisabled();
    });
  });

  it('confirms before saving and reverts dirty edits', async () => {
    stubGet(DOC);
    renderEditor();
    const area = (await screen.findByTestId('config-editor-models-text')) as HTMLTextAreaElement;
    expect(area).not.toBeDisabled();
    fireEvent.change(area, { target: { value: '{"primary": "other"}' } });
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('config-editor-models-revert'));
    expect(
      (screen.getByTestId('config-editor-models-text') as HTMLTextAreaElement).value,
    ).toContain('"primary": "fake"');
    fireEvent.change(screen.getByTestId('config-editor-models-text'), {
      target: { value: '{"primary": "other"}' },
    });
    fireEvent.click(screen.getByTestId('config-editor-models-save'));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Replace models on backend?');
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(screen.getByTestId('toasts')).toHaveTextContent('Saved.');
    });
  });

  it('reports invalid JSON inline with a line number', async () => {
    stubGet(DOC);
    renderEditor();
    await waitFor(() => {
      expect(screen.getByTestId('config-editor-models-text')).not.toBeDisabled();
    });
    fireEvent.change(screen.getByTestId('config-editor-models-text'), {
      target: { value: '{bad json' },
    });
    fireEvent.click(screen.getByTestId('config-editor-models-save'));
    fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    await waitFor(() => {
      expect(screen.getByTestId('config-editor-models-json-error')).toHaveTextContent(/line 1/);
    });
    expect(screen.getByTestId('config-editor-models-text')).toHaveAttribute('aria-invalid', 'true');
  });

  it('filters top-level keys with a match count', async () => {
    stubGet(DOC);
    renderEditor();
    await waitFor(() => {
      expect(screen.getByTestId('config-editor-models-key-count')).toBeInTheDocument();
    });
    expect(screen.getByTestId('config-editor-models-key-count')).toHaveTextContent('2 of 2 keys');
    fireEvent.change(screen.getByTestId('search-input-field'), {
      target: { value: 'primary' },
    });
    expect(screen.getByTestId('config-editor-models-key-count')).toHaveTextContent('1 of 2 keys');
  });
});
