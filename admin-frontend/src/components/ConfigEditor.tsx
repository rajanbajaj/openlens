import { useEffect, useState } from 'react';
import { fetchAdminConfig, saveAdminConfig, SearchApiError } from '../api/client.ts';
import type { AdminConfigDoc, AdminConfigName } from '../api/types.ts';
import { Badge } from './ui/Badge.tsx';
import { Button } from './ui/Button.tsx';
import { Card } from './ui/Card.tsx';

/** Props for one config document editor. */
export interface ConfigEditorProps {
  name: AdminConfigName;
  title: string;
  description: string;
  adminToken: string;
  /** File-seed defaults shown while the backend route is pending. */
  fileDefault: Record<string, unknown>;
}

/**
 * Editor for one DB-backed admin config document. Loads the effective
 * document (DB row when written, YAML seed otherwise) and saves edits
 * through the `X-Admin-Token` gate. While the backend
 * `/api/v1/admin/config/*` routes are still pending, the GET degrades
 * to `null` and the editor shows the file default with a notice; a
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
  const [text, setText] = useState(() => JSON.stringify(fileDefault, null, 2));
  const [notice, setNotice] = useState<string | null>(null);
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
          setText(JSON.stringify(loadedDoc.body, null, 2));
        } else {
          setPendingBackend(true);
          setNotice('Backend config endpoint pending: showing file defaults (read-only preview).');
        }
      },
      () => {
        if (!cancelled) {
          setLoaded(true);
          setPendingBackend(true);
          setNotice('Backend config endpoint pending: showing file defaults (read-only preview).');
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [name]);

  async function handleSave() {
    let parsed: Record<string, unknown> | unknown[];
    try {
      const value: unknown = JSON.parse(text);
      // Mapping sections plus the list-form `plugins:` section.
      if (typeof value !== 'object' || value === null) {
        setNotice('Document must be a JSON object or array.');
        return;
      }
      parsed = value as Record<string, unknown> | unknown[];
    } catch {
      setNotice('Invalid JSON: fix the document before saving.');
      return;
    }
    try {
      const saved = await saveAdminConfig(name, parsed, fetch, adminToken);
      setDoc(saved);
      setPendingBackend(false);
      setNotice('Saved.');
    } catch (error) {
      if (error instanceof SearchApiError) {
        setNotice(`Save failed (${error.code}): ${error.message}`);
      } else {
        setNotice('Save failed.');
      }
    }
  }

  return (
    <Card data-testid={`config-editor-${name}`}>
      <h3 className="text-ol-m">{title}</h3>
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
      {notice ? (
        <p data-testid={`config-editor-${name}-notice`} className="my-ol-s text-ol-s">
          {notice}
        </p>
      ) : null}
      <label className="flex flex-col gap-ol-xs text-ol-s font-semibold">
        Document (JSON object or array)
        <textarea
          data-testid={`config-editor-${name}-text`}
          value={text}
          onChange={(event) => setText(event.target.value)}
          rows={14}
          spellCheck={false}
          className="font-mono font-normal text-ol-ink bg-white/55 border border-ol-glass-border rounded-ol-m px-ol-s py-ol-m"
        />
      </label>
      <div className="mt-ol-s">
        <Button variant="secondary" onClick={() => void handleSave()} disabled={!loaded}>
          Save {name}
        </Button>
      </div>
    </Card>
  );
}
