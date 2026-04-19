/** WebSocket connection + subscription manager. [G-34] */

import { getToken } from "./client";

interface Subscription {
  id: string;
  filter: {
    entity_type?: string;
    entity_id?: string;
    collection?: string;
  };
  callback: (change: EntityChange) => void;
}

export interface EntityChange {
  type: string;
  subscription_id: string;
  collection: string;
  operation: string;
  entity_type: string;
  entity_id: string;
  data: Record<string, unknown>;
}

const MAX_RECONNECT_ATTEMPTS = 20;

class WebSocketManager {
  private ws: WebSocket | null = null;
  private subscriptions = new Map<string, Subscription>();
  private reconnectAttempts = 0;
  private pingInterval: ReturnType<typeof setInterval> | null = null;
  private _disconnected = false;

  get isDisconnected() {
    return this._disconnected;
  }

  connect() {
    const token = getToken();
    if (!token) return;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${window.location.host}/ws?token=${token}`;
    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this.reconnectAttempts = 0;
      this._disconnected = false;
      this.resyncSubscriptions();
      // Keepalive pings — Railway drops idle connections at 60s
      this.pingInterval = setInterval(() => {
        if (this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: "ping" }));
        }
      }, 30000);
    };

    this.ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === "pong") return;
      for (const sub of this.subscriptions.values()) {
        if (this.matchesFilter(data, sub.filter)) {
          sub.callback(data);
        }
      }
    };

    this.ws.onclose = () => {
      if (this.pingInterval) clearInterval(this.pingInterval);
      if (this.reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
        this._disconnected = true;
        console.warn(`WebSocket: gave up after ${MAX_RECONNECT_ATTEMPTS} attempts`);
        return;
      }
      const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), 30000);
      this.reconnectAttempts++;
      setTimeout(() => this.connect(), delay);
    };
  }

  subscribe(
    filter: Subscription["filter"],
    callback: (change: EntityChange) => void
  ): string {
    const id = crypto.randomUUID();
    this.subscriptions.set(id, { id, filter, callback });
    this.sendSubscription(id, filter);
    return id;
  }

  unsubscribe(id: string) {
    this.subscriptions.delete(id);
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "unsubscribe", subscription_id: id }));
    }
  }

  /** Update an existing subscription's filter. [G-34] */
  updateSubscription(id: string, newFilter: Subscription["filter"]) {
    const sub = this.subscriptions.get(id);
    if (sub) {
      sub.filter = newFilter;
      this.sendSubscription(id, newFilter);
    }
  }

  disconnect() {
    if (this.pingInterval) clearInterval(this.pingInterval);
    this.ws?.close();
    this.ws = null;
  }

  private sendSubscription(id: string, filter: Subscription["filter"]) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(
        JSON.stringify({ type: "subscribe", subscription_id: id, filter })
      );
    }
  }

  private resyncSubscriptions() {
    for (const sub of this.subscriptions.values()) {
      this.sendSubscription(sub.id, sub.filter);
    }
  }

  private matchesFilter(
    data: EntityChange,
    filter: Subscription["filter"]
  ): boolean {
    if (filter.entity_type && data.entity_type !== filter.entity_type)
      return false;
    if (filter.entity_id && data.entity_id !== filter.entity_id) return false;
    if (filter.collection && data.collection !== filter.collection) return false;
    return true;
  }
}

export const wsManager = new WebSocketManager();
