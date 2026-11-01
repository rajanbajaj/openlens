import { useEffect, useRef, useState } from 'react';
import { Button } from './ui/Button.tsx';

/**
 * Minimal structural surface of the Web Speech API recognition
 * object. Declared locally (instead of relying on `lib.dom`) so the
 * build stays green on TypeScript configs without the speech types,
 * and so tests can substitute a mock constructor.
 */
interface SpeechAlternative {
  transcript: string;
}

interface SpeechResultEvent {
  resultIndex: number;
  results: ArrayLike<ArrayLike<SpeechAlternative>>;
}

interface SpeechErrorEvent {
  error: string;
}

interface SpeechRecognizer {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((event: SpeechResultEvent) => void) | null;
  onerror: ((event: SpeechErrorEvent) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

type SpeechRecognizerCtor = new () => SpeechRecognizer;

/**
 * Speech-recognition constructor when the browser provides one: the
 * standard `SpeechRecognition` name first, then the WebKit-prefixed
 * name Chrome/Edge/Safari ship. `null` when unsupported (Firefox,
 * jsdom, SSR) — callers render nothing in that case.
 */
export function getSpeechRecognizerCtor(): SpeechRecognizerCtor | null {
  if (typeof window === 'undefined') return null;
  const scope = window as unknown as Record<string, unknown>;
  const ctor = scope['SpeechRecognition'] ?? scope['webkitSpeechRecognition'];
  return typeof ctor === 'function' ? (ctor as SpeechRecognizerCtor) : null;
}

/** Whether this browser can transcribe speech into the search box. */
export function isVoiceInputSupported(): boolean {
  return getSpeechRecognizerCtor() !== null;
}

export interface VoiceInputProps {
  /** Receives the final transcript; the parent owns the input value. */
  onTranscript: (text: string) => void;
  /** Disables the button while a search is in flight. */
  disabled?: boolean;
}

/**
 * Microphone toggle for the search box. Feature-detected: renders
 * nothing where the Web Speech API is absent, so the form works
 * unchanged there. A single utterance (`continuous: false`) is
 * transcribed and handed to `onTranscript` — the user reviews and
 * submits it, so misrecognitions are correctable. Clicking again
 * while listening stops recognition. `onend` always resets the
 * listening state, including after errors, so the button never
 * sticks. Recognition is aborted on unmount.
 */
export default function VoiceInput({ onTranscript, disabled = false }: VoiceInputProps) {
  const [supported] = useState(isVoiceInputSupported);
  const [listening, setListening] = useState(false);
  const recognizerRef = useRef<SpeechRecognizer | null>(null);
  const callbackRef = useRef(onTranscript);
  callbackRef.current = onTranscript;

  useEffect(
    () => () => {
      recognizerRef.current?.abort();
      recognizerRef.current = null;
    },
    [],
  );

  if (!supported) return null;

  function reset() {
    recognizerRef.current = null;
    setListening(false);
  }

  function start() {
    const Ctor = getSpeechRecognizerCtor();
    if (!Ctor || recognizerRef.current) return;
    const recognition = new Ctor();
    recognition.lang =
      typeof navigator !== 'undefined' && navigator.language ? navigator.language : 'en-US';
    recognition.interimResults = false;
    recognition.continuous = false;
    recognition.onresult = (event: SpeechResultEvent) => {
      const text = event.results?.[event.resultIndex]?.[0]?.transcript?.trim();
      if (text) callbackRef.current(text);
    };
    // Errors always precede `onend`; resetting in both is idempotent
    // and covers engines that skip one of the callbacks.
    recognition.onerror = () => reset();
    recognition.onend = () => reset();
    recognizerRef.current = recognition;
    try {
      recognition.start();
      setListening(true);
    } catch {
      // `start()` throws when recognition is already active: drop
      // the instance so the next click starts fresh.
      reset();
    }
  }

  return (
    <Button
      variant="secondary"
      type="button"
      data-testid="voice-input"
      aria-label={listening ? 'Stop voice input' : 'Search by voice'}
      aria-pressed={listening}
      title={listening ? 'Listening… click to stop' : 'Search by voice'}
      disabled={disabled}
      onClick={() => {
        if (listening) recognizerRef.current?.stop();
        else start();
      }}
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <rect x="6" y="1" width="4" height="8" rx="2" />
        <path d="M3 7a5 5 0 0 0 10 0" />
        <path d="M8 12v3" />
      </svg>
    </Button>
  );
}
