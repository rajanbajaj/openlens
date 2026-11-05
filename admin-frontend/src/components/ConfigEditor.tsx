import { useEffect, useMemo, useRef, useState } from 'react';
import { fetchAdminConfig, saveAdminConfig, SearchApiError } from '../api/client.ts';
import type { AdminConfigDoc, AdminConfigName } from '../api/types.ts';
import { Badge } from './ui/Badge.tsx';
import { Button } from './ui/Button.tsx';
import { Card } from './ui/Card.tsx';
import { ConfigGuide } from './ConfigGuide.tsx';
import { ConfirmDialog } from './ui/ConfirmDialog.tsx';
import { FieldError } from './ui/FieldError.tsx';
import { SearchInput } from './ui/SearchInput.tsx';
import { Skeleton } from './ui/Skeleton.tsx';
import { Toasts, useToasts } from './ui/Toast.tsx';

/** Props for one config document editor. */
export interface ConfigEditorProps {
  name: AdminConfigName;
  title: string;
  description: string;
  adminToken: string;
  /** File-seed defaults shown while the backend route is pending. */
  fileDefault: Record<string, unknown>;
}

/** Locate the line number of a JSON parse error (V8 "at position N"). */
function errorLine(text: string, message: string): number | null {
  const match = /at position (\d+)/.exec(message);
  if (!match) return null;
  const offset = Number(match[1]);
  if (!Number.isInteger(offset) || offset < 0) return null;
  return text.slice(0, offset).split('\n').length;
}

/**
 * Editor for one DB-backed admin config document. Loads the effective
 * document (DB row when written, YAML seed otherwise) and saves edits
 * through the `X-Admin-Token` gate. While the backend
 * `/api/v1/admin/config/*` routes are still pending, the GET degrades
 * to `null` and the editor shows the file default with an info toast; a
 * save then surfaces the backend error instead of silently dropping
 * the edit.
 */
export function ConfigEditor({
  name,
  title,
  description,
  adminToken,
  fileDefault,
}: ConfigEditorProps) {
  const [doc, setDoc] = useState<AdminConfigDoc | null>(null);
  const [savedText, setSavedText] = useState(() => JSON.stringify(fileDefault, null, 2));
  const [text, setText] = useState(() => JSON.stringify(fileDefault, null, 2));
  const [keyFilter, setKeyFilter] = useState('');
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [confirmSave, setConfirmSave] = useState(false);
  const previewRef = useRef<HTMLPreElement>(null);
  const { items: toasts, push: pushToast, dismiss: dismissToast } = useToasts();
  const [pendingBackend, setPendingBackend] = useState(false);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchAdminConfig(name, fetch).then(
      (loadedDoc) => {
        if (cancelled) return;
        setLoaded(true);
        if (loadedDoc) {
          setDoc(loadedDoc);
          const body = JSON.stringify(loadedDoc.body, null, 2);
          setSavedText(body);
          setText(body);
        } else {
          setPendingBackend(true);
          pushToast(
            'Backend config endpoint pending: showing file defaults (read-only preview).',
            'info',
          );
        }
      },
      () => {
        if (!cancelled) {
          setLoaded(true);
          setPendingBackend(true);
          pushToast(
            'Backend config endpoint pending: showing file defaults (read-only preview).',
            'info',
          );
        }
      },
    );
    return () => {
      cancelled = true;
    };
    // pushToast is stable across renders (useCallback), so this still
    // re-subscribes only per document.
  }, [name, pushToast]);

  useEffect(() => {
    setSavedText(JSON.stringify(fileDefault, null, 2));
  }, [name, fileDefault]);

  const dirty = text !== savedText;
  const readOnly = pendingBackend || !loaded;

  /** Top-level key matches for the key filter ("3 of 42 keys"). */
  const keyStats = useMemo(() => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      return null;
    }
    const keys =
      parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)
        ? Object.keys(parsed as Record<string, unknown>)
        : [];
    const query = keyFilter.trim().toLowerCase();
    const shown = query === '' ? keys : keys.filter((key) => key.toLowerCase().includes(query));
    return { keys, shown, total: keys.length, visible: shown.length };
  }, [text, keyFilter]);

  /** Non-matching top-level key lines dim out under the filter. */
  const dimmedLines = useMemo(() => {
    if (!keyStats || keyFilter.trim() === '') return new Set<number>();
    const shown = new Set(keyStats.shown);
    const dimmed = new Set<number>();
    text.split('\n').forEach((line, index) => {
      const match = /^\s*"([^"]+)"\s*:/.exec(line);
      if (match && !shown.has(match[1])) dimmed.add(index);
    });
    return dimmed;
  }, [text, keyFilter, keyStats]);

  async function handleSave() {
    setConfirmSave(false);
    let parsed: Record<string, unknown> | unknown[];
    try {
      const value: unknown = JSON.parse(text);
      // Mapping sections plus the list-form `plugins:` section.
      if (typeof value !== 'object' || value === null) {
        const message = 'Document must be a JSON object or array.';
        setJsonError(message);
        pushToast(message, 'error');
        return;
      }
      parsed = value as Record<string, unknown> | unknown[];
    } catch (error) {
      const raw = error instanceof Error ? error.message : 'Invalid JSON.';
      const line = errorLine(text, raw);
      const message = line
        ? `Invalid JSON (line ${line}): fix the document before saving.`
        : `Invalid JSON: fix the document before saving.`;
      setJsonError(message);
      pushToast(message, 'error');
      return;
    }
    setJsonError(null);
    try {
      const saved = await saveAdminConfig(name, parsed, fetch, adminToken);
      setDoc(saved);
      const body = JSON.stringify(saved.body, null, 2);
      setSavedText(body);
      setText(body);
      setPendingBackend(false);
      pushToast('Saved.', 'success');
    } catch (error) {
      if (error instanceof SearchApiError) {
        pushToast(`Save failed (${error.code}): ${error.message}`, 'error');
      } else {
        pushToast('Save failed.', 'error');
      }
    }
  }

  function handleRevert() {
    setText(savedText);
    setJsonError(null);
  }

  return (
    <Card data-testid={`config-editor-${name}`}>
      <h3 className="text-ol-m">
        {title} {dirty ? <Badge>Unsaved changes</Badge> : null}
      </h3>
      <p className="my-ol-s text-ol-s">{description}</p>
      <p className="my-ol-s text-ol-s">
        {doc ? (
          <>
            <Badge>{doc.source === 'db' ? 'DB override' : 'File seed'}</Badge> version {doc.version}
          </>
        ) : (
          <Badge>{pendingBackend ? 'Backend pending' : 'Loading…'}</Badge>
        )}
      </p>
      {!loaded ? (
        <div aria-live="polite">
          <Skeleton bars={6} />
        </div>
      ) : null}
      <ConfigGuide name={name} />
      <div className="my-ol-s">
        <SearchInput
          value={keyFilter}
          onChange={setKeyFilter}
          placeholder="Filter top-level keys…"
          ariaLabel={`Filter ${name} keys`}
          debounceMs={0}
        />
        {keyStats ? (
          <p data-testid={`config-editor-${name}-key-count`} className="mt-ol-xs text-ol-s">
            {keyStats.visible} of {keyStats.total} keys
          </p>
        ) : null}
      </div>
      <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
        Document (JSON object or array)
        <span className="relative block">
          {/* Line-dimming overlay: a textarea cannot style individual
              lines, so an aria-hidden mirror behind transparent text
              dims non-matching key lines while editing stays native. */}
          <pre
            aria-hidden="true"
            data-testid={`config-editor-${name}-preview`}
            ref={previewRef}
            className="absolute inset-0 overflow-hidden pointer-events-none font-mono font-normal text-ol-ink bg-white border border-transparent rounded-ol-m px-ol-s py-ol-m whitespace-pre-wrap break-words"
          >
            {text.split('\n').map((line, index, all) => (
              <span
                key={index}
                data-testid={
                  dimmedLines.has(index) ? `config-editor-${name}-dimmed-${index}` : undefined
                }
                className={dimmedLines.has(index) ? 'opacity-40' : undefined}
              >
                {line}
                {index < all.length - 1 ? '\n' : ''}
              </span>
            ))}
          </pre>
          <textarea
            data-testid={`config-editor-${name}-text`}
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setJsonError(null);
            }}
            onScroll={(event) => {
              if (previewRef.current) {
                previewRef.current.scrollTop = (event.target as HTMLTextAreaElement).scrollTop;
              }
            }}
            rows={14}
            spellCheck={false}
            disabled={!loaded}
            readOnly={readOnly}
            aria-invalid={jsonError !== null}
            aria-describedby={jsonError ? `config-editor-${name}-json-error` : undefined}
            className="relative w-full font-mono font-normal bg-transparent border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
            style={{ color: 'transparent', caretColor: 'var(--ol-ink)' }}
          />
        </span>
      </label>
      {dimmedLines.size > 0 ? (
        <p className="mt-ol-xs text-ol-s text-ol-ink-soft">
          {dimmedLines.size} line(s) dimmed by the key filter.
        </p>
      ) : null}
      {jsonError ? (
        <FieldError id={`config-editor-${name}-json-error`} message={jsonError} />
      ) : null}
      <div className="mt-ol-s flex flex-wrap items-center gap-ol-s">
        <Button
          variant="primary"
          data-testid={`config-editor-${name}-save`}
          onClick={() => setConfirmSave(true)}
          disabled={!loaded || readOnly}
        >
          Save {name}
        </Button>
        <Button
          variant="secondary"
          data-testid={`config-editor-${name}-revert`}
          onClick={handleRevert}
          disabled={!dirty}
        >
          Revert
        </Button>
      </div>
      <ConfirmDialog
        open={confirmSave}
        title={`Replace ${name} on backend?`}
        body="Whole-document write: the backend validates the edit before saving."
        confirmLabel={`Save ${name}`}
        tone="default"
        onConfirm={() => void handleSave()}
        onCancel={() => setConfirmSave(false)}
      />
      <Toasts items={toasts} onDismiss={dismissToast} />
    </Card>
  );
}
