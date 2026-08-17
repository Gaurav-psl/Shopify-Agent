import { useState, useRef, useCallback, useEffect } from "react";

/**
 * Voice assistant chat widget — visual design + Web Speech API wiring.
 *
 * Visual states: idle / listening / thinking / speaking, each with a
 * distinct color and motion so the current state is readable at a
 * glance without reading any text — important for users who may not
 * be comfortable with typical mic-icon-only interfaces.
 *
 * Props:
 *   onSendMessage(text, languageHint) => Promise<{ reply: string, language: string }>
 *     required — sends text (plus the user's selected language, or "auto")
 *     to your /chat endpoint, which runs classify_intent + the matched
 *     action + generate_reply, and resolves with the reply text AND the
 *     ISO 639-1 language code it was written in, so the widget can select
 *     a matching voice for text-to-speech.
 */

const SpeechRecognitionAPI =
  typeof window !== "undefined"
    ? window.SpeechRecognition || window.webkitSpeechRecognition
    : null;

const STATUS_TEXT = {
  idle: "Tap the mic to talk, or type below",
  listening: "Listening...",
  thinking: "Thinking...",
  speaking: "Speaking...",
};

// Curated list — extend as needed. Codes are BCP-47 for recognition.lang.
const LANGUAGES = [
  { code: "auto", label: "Auto-detect", recognitionLang: null },
  { code: "en", label: "English", recognitionLang: "en-US" },
  { code: "hi", label: "Hindi", recognitionLang: "hi-IN" },
  { code: "pa", label: "Punjabi", recognitionLang: "pa-IN" },
  { code: "ta", label: "Tamil", recognitionLang: "ta-IN" },
  { code: "mr", label: "Marathi", recognitionLang: "mr-IN" },
  { code: "bn", label: "Bengali", recognitionLang: "bn-IN" },
  { code: "te", label: "Telugu", recognitionLang: "te-IN" },
  { code: "gu", label: "Gujarati", recognitionLang: "gu-IN" },
  { code: "kn", label: "Kannada", recognitionLang: "kn-IN" },
  { code: "es", label: "Spanish", recognitionLang: "es-ES" },
  { code: "fr", label: "French", recognitionLang: "fr-FR" },
  { code: "ar", label: "Arabic", recognitionLang: "ar-SA" },
];

function pickVoiceForLanguage(langCode) {
  if (!window.speechSynthesis) return null;
  const voices = window.speechSynthesis.getVoices();
  if (!voices.length) return null;
  // Prefer an exact-ish match on language prefix (e.g. "hi" matches "hi-IN")
  return voices.find((v) => v.lang?.toLowerCase().startsWith(langCode)) || null;
}

export default function VoiceAssistantWidget({ onSendMessage }) {
  const [state, setState] = useState("idle"); // idle | listening | thinking | speaking
  const [messages, setMessages] = useState([
    {
      role: "agent",
      text: "Hi! I can help you track an order, find a product, or answer questions about shipping and returns. What do you need?",
    },
  ]);
  const [inputValue, setInputValue] = useState("");
  const [language, setLanguage] = useState("auto"); // user-selected language preference
  const recognitionRef = useRef(null);
  const transcriptEndRef = useRef(null);
  const supported = !!SpeechRecognitionAPI;

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const speak = useCallback((text, replyLanguage) => {
    if (!window.speechSynthesis) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    const voice = pickVoiceForLanguage(replyLanguage || "en");
    if (voice) utterance.voice = voice;
    utterance.lang = voice?.lang || replyLanguage || "en-US";
    utterance.onstart = () => setState("speaking");
    utterance.onend = () => setState("idle");
    window.speechSynthesis.speak(utterance);
  }, []);

  const handleSend = useCallback(
    async (text) => {
      if (!text.trim()) return;
      setMessages((prev) => [...prev, { role: "user", text }]);
      setState("thinking");
      try {
        const { reply, language: replyLanguage } = await onSendMessage(text, language);
        setMessages((prev) => [...prev, { role: "agent", text: reply }]);
        speak(reply, replyLanguage);
      } catch (err) {
        setMessages((prev) => [
          ...prev,
          { role: "agent", text: "Sorry, something went wrong. Please try again." },
        ]);
        setState("idle");
      }
    },
    [onSendMessage, speak, language]
  );

  const startListening = useCallback(() => {
    if (!SpeechRecognitionAPI) return;
    const recognition = new SpeechRecognitionAPI();
    recognition.continuous = false;
    recognition.interimResults = false;
    const selected = LANGUAGES.find((l) => l.code === language);
    recognition.lang = selected?.recognitionLang || "en-US";

    recognition.onstart = () => setState("listening");
    recognition.onresult = (event) => {
      const text = event.results[0][0].transcript;
      handleSend(text);
    };
    recognition.onerror = () => setState("idle");
    recognition.onend = () => {
      setState((current) => (current === "listening" ? "idle" : current));
    };

    recognitionRef.current = recognition;
    recognition.start();
  }, [handleSend]);

  const stopListening = useCallback(() => {
    recognitionRef.current?.stop();
    setState("idle");
  }, []);

  const handleMicClick = () => {
    if (state === "listening") stopListening();
    else if (state === "idle") startListening();
  };

  const handleTextSubmit = (e) => {
    e.preventDefault();
    handleSend(inputValue);
    setInputValue("");
  };

  return (
    <div className="vaw-root">
      <style>{`
        .vaw-root {
          --paper: #EEF1F6;
          --surface: #FFFFFF;
          --ink: #262A33;
          --muted: #6B7280;
          --sage: #3E6259;
          --sage-tint: #E4EDE9;
          --coral: #E8785A;
          --coral-tint: #FBEAE4;
          --border: #E2E6ED;
          font-family: 'Inter', -apple-system, sans-serif;
          width: 100%;
          max-width: 400px;
        }
        .vaw-widget {
          background: var(--surface);
          border-radius: 28px;
          box-shadow: 0 1px 2px rgba(38,42,51,0.04), 0 16px 40px -12px rgba(38,42,51,0.14);
          border: 1px solid var(--border);
          overflow: hidden;
          display: flex;
          flex-direction: column;
        }
        .vaw-header { padding: 28px 28px 8px; text-align: center; }
        .vaw-eyebrow {
          font-size: 12px; font-weight: 600; letter-spacing: 0.08em;
          text-transform: uppercase; color: var(--sage); margin: 0 0 8px;
        }
        .vaw-headline {
          font-family: Georgia, 'Times New Roman', serif;
          font-size: 24px; font-weight: 500; margin: 0; line-height: 1.25; color: var(--ink);
        }
        .vaw-lang-select {
          margin-top: 14px;
          font-family: 'Inter', sans-serif;
          font-size: 12px;
          font-weight: 500;
          color: var(--sage);
          background: var(--sage-tint);
          border: none;
          border-radius: 999px;
          padding: 6px 14px;
          cursor: pointer;
          outline: none;
        }
        .vaw-lang-select:focus-visible { outline: 2px solid var(--coral); outline-offset: 2px; }
        .vaw-orb-stage { display: flex; flex-direction: column; align-items: center; padding: 20px 0 8px; }
        .vaw-orb-wrap { position: relative; width: 120px; height: 120px; display: flex; align-items: center; justify-content: center; }
        .vaw-ring { position: absolute; width: 78px; height: 78px; border-radius: 50%; border: 1.5px solid var(--sage); opacity: 0; }
        .vaw-orb-wrap[data-state="idle"] .vaw-ring { animation: vaw-breathe-ring 3.6s ease-in-out infinite; }
        .vaw-orb-wrap[data-state="idle"] .vaw-ring:nth-child(1) { animation-delay: 0s; }
        .vaw-orb-wrap[data-state="idle"] .vaw-ring:nth-child(2) { animation-delay: 1.2s; }
        .vaw-orb-wrap[data-state="idle"] .vaw-ring:nth-child(3) { animation-delay: 2.4s; }
        .vaw-orb-wrap[data-state="listening"] .vaw-ring { border-color: var(--coral); animation: vaw-pulse-ring 1.6s ease-out infinite; }
        .vaw-orb-wrap[data-state="listening"] .vaw-ring:nth-child(1) { animation-delay: 0s; }
        .vaw-orb-wrap[data-state="listening"] .vaw-ring:nth-child(2) { animation-delay: 0.5s; }
        .vaw-orb-wrap[data-state="listening"] .vaw-ring:nth-child(3) { animation-delay: 1s; }
        @keyframes vaw-breathe-ring { 0% { transform: scale(0.9); opacity: 0; } 30% { opacity: 0.35; } 70% { opacity: 0; } 100% { transform: scale(1.35); opacity: 0; } }
        @keyframes vaw-pulse-ring { 0% { transform: scale(0.85); opacity: 0.55; } 100% { transform: scale(1.6); opacity: 0; } }
        .vaw-orb {
          position: relative; width: 78px; height: 78px; border-radius: 50%;
          background: radial-gradient(circle at 32% 28%, #5C8377, var(--sage) 65%);
          display: flex; align-items: center; justify-content: center;
          box-shadow: 0 8px 24px -6px rgba(62,98,89,0.45);
          transition: background 0.4s ease, box-shadow 0.4s ease, transform 0.3s ease;
          animation: vaw-breathe-scale 3.6s ease-in-out infinite; z-index: 1;
        }
        .vaw-orb-wrap[data-state="listening"] .vaw-orb {
          background: radial-gradient(circle at 32% 28%, #F0916F, var(--coral) 65%);
          box-shadow: 0 8px 28px -4px rgba(232,120,90,0.55); animation: none; transform: scale(1.04);
        }
        .vaw-orb-wrap[data-state="thinking"] .vaw-orb { animation: vaw-think 1.2s ease-in-out infinite, vaw-breathe-scale 3.6s ease-in-out infinite; }
        .vaw-orb-wrap[data-state="speaking"] .vaw-orb { animation: vaw-speak-pulse 0.5s ease-in-out infinite alternate; }
        @keyframes vaw-breathe-scale { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.05); } }
        @keyframes vaw-speak-pulse { 0% { transform: scale(1); } 100% { transform: scale(1.09); } }
        @keyframes vaw-think { 0%, 100% { filter: brightness(1); } 50% { filter: brightness(1.2); } }
        .vaw-orb svg { width: 30px; height: 30px; }
        .vaw-status { margin-top: 12px; font-size: 14px; font-weight: 500; color: var(--muted); min-height: 20px; }
        .vaw-status.listening { color: var(--coral); }
        .vaw-status.active { color: var(--sage); }
        .vaw-transcript { padding: 8px 24px 4px; display: flex; flex-direction: column; gap: 10px; max-height: 260px; overflow-y: auto; }
        .vaw-bubble { max-width: 84%; padding: 12px 16px; border-radius: 18px; font-size: 15px; line-height: 1.45; }
        .vaw-bubble.user { align-self: flex-end; background: var(--sage-tint); color: var(--ink); border-bottom-right-radius: 6px; }
        .vaw-bubble.agent { align-self: flex-start; background: var(--surface); border: 1px solid var(--border); color: var(--ink); border-bottom-left-radius: 6px; }
        .vaw-controls { padding: 16px 24px 26px; display: flex; flex-direction: column; align-items: center; gap: 12px; border-top: 1px solid var(--border); margin-top: 8px; }
        .vaw-mic-button {
          width: 64px; height: 64px; border-radius: 50%; border: none; background: var(--sage); color: white;
          display: flex; align-items: center; justify-content: center; cursor: pointer;
          box-shadow: 0 6px 18px -4px rgba(62,98,89,0.5); transition: transform 0.15s ease, background 0.3s ease;
        }
        .vaw-mic-button:hover { transform: translateY(-1px); }
        .vaw-mic-button:active { transform: scale(0.96); }
        .vaw-mic-button:focus-visible { outline: 3px solid var(--coral); outline-offset: 3px; }
        .vaw-mic-button.listening { background: var(--coral); }
        .vaw-mic-button:disabled { opacity: 0.4; cursor: not-allowed; }
        .vaw-mic-button svg { width: 24px; height: 24px; }
        .vaw-mic-hint { font-size: 13px; color: var(--muted); }
        .vaw-text-row { width: 100%; display: flex; align-items: center; gap: 8px; background: var(--paper); border: 1px solid var(--border); border-radius: 999px; padding: 6px 6px 6px 18px; }
        .vaw-text-row input { flex: 1; border: none; background: transparent; font-family: 'Inter', sans-serif; font-size: 14px; color: var(--ink); outline: none; }
        .vaw-text-row input::placeholder { color: var(--muted); }
        .vaw-send-button { width: 34px; height: 34px; border-radius: 50%; border: none; background: var(--ink); color: white; display: flex; align-items: center; justify-content: center; cursor: pointer; flex-shrink: 0; }
        .vaw-send-button svg { width: 15px; height: 15px; }
        @media (prefers-reduced-motion: reduce) {
          .vaw-orb, .vaw-ring { animation: none !important; }
        }
      `}</style>

      <div className="vaw-widget">
        <div className="vaw-header">
          <p className="vaw-eyebrow">Your shopping assistant</p>
          <h1 className="vaw-headline">Hi, how can I help today?</h1>
          <select
            className="vaw-lang-select"
            value={language}
            onChange={(e) => setLanguage(e.target.value)}
            aria-label="Choose language"
          >
            {LANGUAGES.map((l) => (
              <option key={l.code} value={l.code}>{l.label}</option>
            ))}
          </select>
        </div>

        <div className="vaw-orb-stage">
          <div className="vaw-orb-wrap" data-state={state}>
            <div className="vaw-ring" />
            <div className="vaw-ring" />
            <div className="vaw-ring" />
            <div className="vaw-orb">
              <svg viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
                <path d="M19 10v1a7 7 0 0 1-14 0v-1" />
                <line x1="12" y1="18" x2="12" y2="22" />
              </svg>
            </div>
          </div>
          <p className={`vaw-status ${state === "listening" ? "listening" : state !== "idle" ? "active" : ""}`}>
            {STATUS_TEXT[state]}
          </p>
        </div>

        <div className="vaw-transcript">
          {messages.map((m, i) => (
            <div key={i} className={`vaw-bubble ${m.role}`}>{m.text}</div>
          ))}
          <div ref={transcriptEndRef} />
        </div>

        <div className="vaw-controls">
          <button
            className={`vaw-mic-button ${state === "listening" ? "listening" : ""}`}
            onClick={handleMicClick}
            disabled={!supported || state === "thinking" || state === "speaking"}
            aria-label={state === "listening" ? "Stop listening" : "Tap to talk"}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
              <path d="M19 10v1a7 7 0 0 1-14 0v-1" />
              <line x1="12" y1="18" x2="12" y2="22" />
            </svg>
          </button>
          <span className="vaw-mic-hint">
            {supported ? STATUS_TEXT[state] : "Voice not supported — please type"}
          </span>

          <form className="vaw-text-row" onSubmit={handleTextSubmit}>
            <input
              type="text"
              placeholder="Or type your question..."
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
            />
            <button type="submit" className="vaw-send-button" aria-label="Send">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="22" y1="2" x2="11" y2="13" />
                <polygon points="22 2 15 22 11 13 2 9 22 2" />
              </svg>
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
