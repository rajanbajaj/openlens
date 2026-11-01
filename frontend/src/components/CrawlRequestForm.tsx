import { useState } from 'react';
import { requestCrawlUrl } from '../api/client.ts';
import { Button } from './ui/Button.tsx';
import { TextInput } from './ui/TextInput.tsx';

export interface CrawlRequestFormProps {
  /** Disables the form while a search is in flight. */
  disabled?: boolean;
}

/**
 * "Request a URL": lets a reader ask for a specific page to be
 * crawled. Posts to the public (rate-limited) crawl-request endpoint;
 * an admin approves or declines it on the frontier dashboard. Success,
 * duplicate, validation, and transport states all render inline —
 * nothing throws into the surrounding page.
 */
export function CrawlRequestForm({ disabled = false }: CrawlRequestFormProps) {
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [isError, setIsError] = useState(false);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || disabled) return;
    setBusy(true);
    setNotice(null);
    const outcome = await requestCrawlUrl(url);
    setBusy(false);
    if (outcome.ok) {
      setIsError(false);
      setNotice(
        outcome.status === 'duplicate'
          ? 'Already in the index queue — no new request needed.'
          : 'Queued for review — an admin approves crawl requests.',
      );
      setUrl('');
    } else {
      setIsError(true);
      setNotice(outcome.message);
    }
  }

  return (
    <form
      aria-label="Request a URL for crawling"
      data-testid="crawl-request-form"
      onSubmit={(event) => {
        void submit(event);
      }}
    >
      <TextInput
        label="Page URL"
        id="crawl-request-url"
        name="crawl_request_url"
        type="url"
        placeholder="https://example.com/article"
        value={url}
        disabled={disabled || busy}
        onChange={(event) => setUrl(event.target.value)}
      />
      <Button
        type="submit"
        variant="secondary"
        data-testid="crawl-request-submit"
        disabled={disabled || busy || url.trim() === ''}
      >
        {busy ? 'Queueing…' : 'Request crawl'}
      </Button>
      {notice ? (
        <p data-testid="crawl-request-notice" role="status" aria-live="polite">
          {isError ? `Could not queue: ${notice}` : notice}
        </p>
      ) : null}
    </form>
  );
}
