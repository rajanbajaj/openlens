import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import VoiceInput from './VoiceInput.tsx';

function scope(): Record<string, unknown> {
  return window as unknown as Record<string, unknown>;
}

/** Minimal mock of the speech recognizer the component drives. */
class MockRecognition {
  static instances: MockRecognition[] = [];
  lang = '';
  interimResults = true;
  continuous = true;
  onresult: ((event: unknown) => void) | null = null;
  onerror: (() => void) | null = null;
  onend: (() => void) | null = null;
  started = false;
  stopped = false;
  constructor() {
    MockRecognition.instances.push(this);
  }
  start() {
    this.started = true;
  }
  stop() {
    this.stopped = true;
  }
  abort() {}
}

afterEach(() => {
  cleanup();
  MockRecognition.instances = [];
  delete scope()['SpeechRecognition'];
  delete scope()['webkitSpeechRecognition'];
});

function installMock(prefixed = false) {
  if (prefixed) scope()['webkitSpeechRecognition'] = MockRecognition;
  else scope()['SpeechRecognition'] = MockRecognition;
}

function latest(): MockRecognition {
  const instance = MockRecognition.instances[MockRecognition.instances.length - 1];
  if (!instance) throw new Error('no recognizer instance created');
  return instance;
}

describe('VoiceInput', () => {
  it('renders nothing where speech recognition is unsupported', () => {
    render(<VoiceInput onTranscript={() => {}} />);
    expect(screen.queryByTestId('voice-input')).toBeNull();
  });

  it('uses the prefixed constructor when only it exists', () => {
    installMock(true);
    render(<VoiceInput onTranscript={() => {}} />);
    expect(screen.getByTestId('voice-input')).toHaveAttribute('aria-label', 'Search by voice');
  });

  it('starts listening on click and reports the pressed state', () => {
    installMock();
    render(<VoiceInput onTranscript={() => {}} />);
    const button = screen.getByTestId('voice-input');
    expect(button).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(button);
    expect(latest().started).toBe(true);
    expect(button).toHaveAttribute('aria-pressed', 'true');
    expect(button).toHaveAttribute('aria-label', 'Stop voice input');
  });

  it('delivers the trimmed final transcript to the parent', () => {
    installMock();
    const onTranscript = vi.fn();
    render(<VoiceInput onTranscript={onTranscript} />);
    fireEvent.click(screen.getByTestId('voice-input'));
    latest().onresult?.({
      resultIndex: 0,
      results: [[{ transcript: '  hello world  ' }]],
    });
    expect(onTranscript).toHaveBeenCalledWith('hello world');
  });

  it('stops on a second click and resets when recognition ends', () => {
    installMock();
    render(<VoiceInput onTranscript={() => {}} />);
    const button = screen.getByTestId('voice-input');
    fireEvent.click(button);
    fireEvent.click(button);
    expect(latest().stopped).toBe(true);
    act(() => {
      latest().onend?.();
    });
    expect(button).toHaveAttribute('aria-pressed', 'false');
  });

  it('resets the listening state on recognition errors', () => {
    installMock();
    render(<VoiceInput onTranscript={() => {}} />);
    const button = screen.getByTestId('voice-input');
    fireEvent.click(button);
    expect(button).toHaveAttribute('aria-pressed', 'true');
    act(() => {
      latest().onerror?.();
    });
    expect(button).toHaveAttribute('aria-pressed', 'false');
  });
});
