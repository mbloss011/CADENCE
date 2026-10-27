// One WebSocket for the whole app, with exponential-backoff reconnect.
export type ServerMsg = { kind: string; [k: string]: unknown };
type Listener = (msg: ServerMsg) => void;
type StateListener = (connected: boolean) => void;

class Socket {
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private stateListeners = new Set<StateListener>();
  private queue: string[] = [];
  private backoff = 500;
  connected = false;

  connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/ws`);
    this.ws = ws;
    ws.onopen = () => {
      this.backoff = 500;
      this.setConnected(true);
      for (const m of this.queue.splice(0)) ws.send(m);
    };
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data) as ServerMsg;
        this.listeners.forEach((l) => l(msg));
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onclose = () => {
      this.setConnected(false);
      setTimeout(() => this.connect(), this.backoff);
      this.backoff = Math.min(this.backoff * 2, 8000);
    };
  }

  private setConnected(v: boolean) {
    this.connected = v;
    this.stateListeners.forEach((l) => l(v));
  }

  send(data: Record<string, unknown>) {
    const raw = JSON.stringify(data);
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(raw);
    else this.queue.push(raw);
  }

  on(l: Listener) {
    this.listeners.add(l);
    return () => void this.listeners.delete(l);
  }

  onState(l: StateListener) {
    this.stateListeners.add(l);
    return () => void this.stateListeners.delete(l);
  }
}

export const socket = new Socket();
