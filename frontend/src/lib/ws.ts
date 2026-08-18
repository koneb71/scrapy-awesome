import { useEffect, useRef, useState } from "react";
import type { RunEvent } from "./types";

/** Live events for one run over WebSocket (auto-reconnect while the run is active). */
export function useRunEvents(runId: string | undefined, onEvent?: (e: RunEvent) => void) {
  const [connected, setConnected] = useState(false);
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    if (!runId) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let attempt = 0;
    let timer: number | undefined;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws/runs/${runId}`);
      ws.onopen = () => {
        attempt = 0;
        setConnected(true);
      };
      ws.onmessage = (m) => {
        try {
          const ev = JSON.parse(m.data) as RunEvent;
          if (ev.t === "ping") return;
          handler.current?.(ev);
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (closed) return;
        attempt += 1;
        timer = window.setTimeout(connect, Math.min(1000 * 2 ** attempt, 10_000));
      };
      ws.onerror = () => ws?.close();
    };
    connect();
    return () => {
      closed = true;
      if (timer) window.clearTimeout(timer);
      ws?.close();
    };
  }, [runId]);

  return connected;
}

/** App-wide firehose (`/ws/events`): agent hand-offs (pick requests, navigate) + run events. */
export function useAppEvents(onEvent: (e: Record<string, unknown> & { t: string }) => void) {
  const handler = useRef(onEvent);
  handler.current = onEvent;
  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let attempt = 0;
    let timer: number | undefined;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws/events`);
      ws.onopen = () => {
        attempt = 0;
      };
      ws.onmessage = (m) => {
        try {
          const ev = JSON.parse(m.data);
          if (ev?.t && ev.t !== "ping") handler.current(ev);
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        if (closed) return;
        attempt += 1;
        timer = window.setTimeout(connect, Math.min(1000 * 2 ** attempt, 15_000));
      };
      ws.onerror = () => ws?.close();
    };
    connect();
    return () => {
      closed = true;
      if (timer) window.clearTimeout(timer);
      ws?.close();
    };
  }, []);
}

/** Events for one designer chat (`/ws/chats/{id}`); reconnects while mounted. */
export function useChatEvents(chatId: string | null | undefined, onEvent: (e: Record<string, unknown> & { t: string }) => void) {
  const handler = useRef(onEvent);
  handler.current = onEvent;
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    if (!chatId) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let attempt = 0;
    let timer: number | undefined;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws/chats/${chatId}`);
      ws.onopen = () => {
        attempt = 0;
        setConnected(true);
      };
      ws.onmessage = (m) => {
        try {
          const ev = JSON.parse(m.data);
          if (ev?.t && ev.t !== "ping") handler.current(ev);
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (closed) return;
        attempt += 1;
        timer = window.setTimeout(connect, Math.min(1000 * 2 ** attempt, 15_000));
      };
      ws.onerror = () => ws?.close();
    };
    connect();
    return () => {
      closed = true;
      if (timer) window.clearTimeout(timer);
      ws?.close();
    };
  }, [chatId]);
  return connected;
}
