/**
 * AG-UI HttpAgent Demo — Raw AG-UI client consuming our bridge.
 *
 * This shows how to use @ag-ui/client's HttpAgent directly to connect
 * to the bridge's POST /ag-ui endpoint. No CopilotKit needed — just
 * the raw AG-UI protocol client and a simple React UI.
 *
 * This is the "build your own UI" approach — full control, no framework.
 */

import { useState, useCallback } from "react";
import { HttpAgent } from "@ag-ui/client";
import { EventType } from "@ag-ui/core";
import ReactMarkdown from "react-markdown";

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
}

const BRIDGE_URL = "http://localhost:8000/ag-ui";

export default function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);

  const sendMessage = useCallback(async () => {
    if (!input.trim() || streaming) return;

    const userMsg: Message = { id: `msg-${Date.now()}`, role: "user", content: input };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setStreaming(true);

    const assistantId = `msg-${Date.now()}-assistant`;
    setMessages((prev) => [...prev, { id: assistantId, role: "assistant", content: "" }]);

    // Create HttpAgent pointing at our bridge
    const agent = new HttpAgent({ url: BRIDGE_URL });

    try {
      // HttpAgent.run() returns an Observable of AG-UI events
      const observable = agent.run({
        threadId: "demo-thread",
        runId: `run-${Date.now()}`,
        messages: [...messages, userMsg].map((m) => ({
          id: m.id,
          role: m.role,
          content: m.content,
        })),
        tools: [],
        state: {},
        context: [],
        forwardedProps: { cwd: "." },
      });

      observable.subscribe({
        next: (event: any) => {
          if (event.type === EventType.TEXT_MESSAGE_CONTENT) {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId
                  ? { ...m, content: m.content + event.delta }
                  : m
              )
            );
          }
        },
        error: (err: any) => {
          console.error("Stream error:", err);
          setStreaming(false);
        },
        complete: () => {
          setStreaming(false);
        },
      });
    } catch (err) {
      console.error("Failed to run agent:", err);
      setStreaming(false);
    }
  }, [input, messages, streaming]);

  return (
    <div style={{ maxWidth: 700, margin: "0 auto", padding: 24, fontFamily: "system-ui" }}>
      <h1 style={{ fontSize: 20, marginBottom: 4 }}>AG-UI HttpAgent + ACP Agent</h1>
      <p style={{ color: "#6b7280", fontSize: 13, marginBottom: 24 }}>
        Using <code>@ag-ui/client</code> HttpAgent directly — no CopilotKit, just the raw protocol.
      </p>

      <div style={{ border: "1px solid #e5e7eb", borderRadius: 8, minHeight: 400, padding: 16, marginBottom: 16, overflow: "auto" }}>
        {messages.length === 0 && (
          <p style={{ color: "#9ca3af", textAlign: "center", marginTop: 160 }}>
            Send a message to your ACP agent...
          </p>
        )}
        {messages.map((msg) => (
          <div
            key={msg.id}
            style={{
              marginBottom: 12,
              padding: "8px 12px",
              borderRadius: 8,
              background: msg.role === "user" ? "#eff6ff" : "#f9fafb",
              borderLeft: msg.role === "user" ? "3px solid #3b82f6" : "3px solid #10b981",
            }}
          >
            <div style={{ fontSize: 11, color: "#6b7280", marginBottom: 4 }}>
              {msg.role === "user" ? "You" : "Agent"}
            </div>
            <div style={{ fontSize: 14 }}>
              {msg.role === "assistant" ? (
                <ReactMarkdown>{msg.content || "..."}</ReactMarkdown>
              ) : (
                msg.content
              )}
            </div>
          </div>
        ))}
      </div>

      <div style={{ display: "flex", gap: 8 }}>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && sendMessage()}
          placeholder="Type a message..."
          disabled={streaming}
          style={{
            flex: 1,
            padding: "10px 14px",
            borderRadius: 8,
            border: "1px solid #d1d5db",
            fontSize: 14,
            outline: "none",
          }}
        />
        <button
          onClick={sendMessage}
          disabled={streaming || !input.trim()}
          style={{
            padding: "10px 20px",
            borderRadius: 8,
            background: streaming ? "#9ca3af" : "#3b82f6",
            color: "white",
            border: "none",
            fontSize: 14,
            cursor: streaming ? "not-allowed" : "pointer",
          }}
        >
          {streaming ? "..." : "Send"}
        </button>
      </div>

      <div style={{ marginTop: 16, padding: 12, background: "#f3f4f6", borderRadius: 8, fontSize: 12, color: "#6b7280" }}>
        <strong>How this works:</strong> This React app uses <code>@ag-ui/client</code>'s HttpAgent
        to POST to <code>{BRIDGE_URL}</code>. The bridge spawns your ACP agent, translates
        JSON-RPC notifications to AG-UI events, and streams them back. No CopilotKit — just the
        raw AG-UI protocol.
      </div>
    </div>
  );
}
