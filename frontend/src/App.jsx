import React, { useEffect, useRef, useState } from "react";
import { API_URL, sendChat } from "./api.js";

function newSessionId() {
  const rand =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID().replace(/-/g, "").slice(0, 8)
      : Math.random().toString(16).slice(2, 10);
  return `s-${rand}`;
}

function Stat({ label, testId, children }) {
  return (
    <div className="stat">
      <span className="stat-value" data-testid={testId}>
        {children}
      </span>
      <span className="stat-label">{label}</span>
    </div>
  );
}

function AssistantMessage({ msg }) {
  const m = msg.metadata;
  const cached = m.resolution_source === "LLM_CACHE";
  return (
    <article className={`msg assistant ${cached ? "from-cache" : "from-live"}`} data-testid="assistant-message">
      <p className="reply">{msg.text}</p>
      <div className="stats">
        <Stat label="served from" testId="meta-source">
          {m.resolution_source}
        </Stat>
        <Stat label="model" testId="meta-model">
          {m.model}
        </Stat>
        <Stat label="latency" testId="meta-latency">
          {`${m.latency_ms} ms`}
        </Stat>
        <Stat label="input tokens" testId="meta-input-tokens">
          {m.input_tokens}
        </Stat>
        <Stat label="output tokens" testId="meta-output-tokens">
          {m.output_tokens}
        </Stat>
      </div>
      <p className="route" data-testid="meta-route">
        {cached
          ? "Answered from cache, no key used."
          : `Answered by ${m.key_id}` +
            (m.fallback_attempts > 0
              ? ` after ${m.fallback_attempts} failed key${m.fallback_attempts > 1 ? "s" : ""}.`
              : ".")}
      </p>
    </article>
  );
}

export default function App() {
  const [sessionId, setSessionId] = useState(newSessionId); // a new session on every page load
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [simulate, setSimulate] = useState("");
  const [pending, setPending] = useState(false);
  const endRef = useRef(null);
  const nextId = useRef(0);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
  }, [messages, pending]);

  const add = (m) => setMessages((prev) => [...prev, { id: nextId.current++, ...m }]);

  async function send() {
    const text = input.trim();
    if (!text || pending) return;
    setInput(""); // the input is empty as soon as the message is sent
    add({ kind: "user", text });
    setPending(true);
    try {
      const data = await sendChat({ sessionId, message: text, systemPrompt, simulateFailure: simulate });
      add({ kind: "assistant", text: data.reply, metadata: data.metadata });
    } catch (err) {
      add({ kind: "error", text: err.message });
    } finally {
      setPending(false);
    }
  }

  function onKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  }

  function resetChat() {
    setMessages([]);
    setSessionId(newSessionId());
  }

  return (
    <div className="app">
      <header className="top">
        <div>
          <h1>Relay Chat</h1>
          <p className="sub">Every reply shows how it was served: cache or live, which model, how long, how many tokens.</p>
        </div>
        <div className="session">
          <span className="session-id" title={`Backend: ${API_URL}`} data-testid="session-id">
            {sessionId}
          </span>
          <button type="button" className="ghost" onClick={resetChat}>
            New chat
          </button>
        </div>
      </header>

      <details className="options">
        <summary>Session options</summary>
        <label htmlFor="sys">System prompt for this session</label>
        <textarea
          id="sys"
          data-testid="system-prompt-input"
          rows={2}
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
          placeholder="Leave empty to use the server default."
        />
        <label htmlFor="sim">Simulate key failures (testing)</label>
        <input
          id="sim"
          data-testid="simulate-failure-input"
          value={simulate}
          onChange={(e) => setSimulate(e.target.value)}
          placeholder="key_1=429   or   key_1=429,key_2=500   or   *=429"
        />
      </details>

      <main className="thread" aria-live="polite">
        {messages.length === 0 && (
          <div className="empty">
            <p>Ask anything. Send the same first message in a new chat to see a cache hit.</p>
          </div>
        )}
        {messages.map((m) =>
          m.kind === "user" ? (
            <article key={m.id} className="msg user" data-testid="user-message">
              <p>{m.text}</p>
            </article>
          ) : m.kind === "assistant" ? (
            <AssistantMessage key={m.id} msg={m} />
          ) : (
            <article key={m.id} className="msg error" role="alert" data-testid="error-message">
              <p>{m.text}</p>
            </article>
          )
        )}
        {pending && (
          <div className="thinking" data-testid="pending">
            Waiting for a reply…
          </div>
        )}
        <div ref={endRef} />
      </main>

      <footer className="composer">
        <textarea
          data-testid="chat-input"
          rows={2}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Write a message. Enter sends, Shift+Enter adds a line."
          aria-label="Message"
        />
        <button type="button" data-testid="send-button" onClick={send} disabled={pending || !input.trim()}>
          Send
        </button>
      </footer>
    </div>
  );
}
