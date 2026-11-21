const raw =
  (typeof window !== "undefined" && window.__APP_CONFIG__ && window.__APP_CONFIG__.API_URL) ||
  import.meta.env.VITE_API_URL ||
  "http://localhost:8000";

export const API_URL = raw.replace(/\/+$/, "");

/** Turn any failure into a short, readable sentence. */
function describeError(status, body) {
  if (status === 503) return "Every AI provider key is busy or unavailable right now. Try again in a moment.";
  if (status === 422) {
    const d = body && body.detail;
    if (Array.isArray(d) && d.length) return "Your message was rejected: " + d.map((e) => e.msg).join("; ");
    if (typeof d === "string") return d;
    return "Your message was rejected as invalid.";
  }
  if (body && typeof body.detail === "string") return body.detail;
  return `The server returned an unexpected error (HTTP ${status}).`;
}

export async function sendChat({ sessionId, message, systemPrompt, simulateFailure }) {
  const headers = { "Content-Type": "application/json" };
  if (simulateFailure && simulateFailure.trim()) headers["X-Simulate-Failure"] = simulateFailure.trim();

  const payload = { session_id: sessionId, message };
  if (systemPrompt && systemPrompt.trim()) payload.system_prompt = systemPrompt.trim();

  let res;
  try {
    res = await fetch(`${API_URL}/chat`, { method: "POST", headers, body: JSON.stringify(payload) });
  } catch {
    throw new Error(`Can't reach the backend at ${API_URL}. Check that it is running and that CORS allows this site.`);
  }

  let body = null;
  try {
    body = await res.json();
  } catch {
    /* non-JSON error page */
  }
  if (!res.ok) throw new Error(describeError(res.status, body));
  return body;
}
