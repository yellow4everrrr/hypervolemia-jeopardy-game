/**
 * Broker connections, transcribed from `app.interfaces.http.schemas.broker`.
 *
 * Note what `BrokerConnectionSummary` does *not* contain: no username, no password, no
 * API secret, no token. The backend's own docstring says "deliberately contains no
 * credential material", and the shape here matches — so there is nothing for a component
 * to render by accident, nothing in a React DevTools tree, and nothing in a cached query.
 *
 * The credential fields exist only on `ConnectBrokerRequest`, which travels one way.
 */

export type ConnectionStatus = "active" | "error" | "revoked" | "expired";

export interface ConnectBrokerRequest {
  broker: "tradovate";
  label: string;
  environment: "demo" | "live";
  username: string;
  password: string;
  /** API key pair from Tradovate Trader → Application Settings → API Access. */
  cid: string;
  secret: string;
  device_id?: string | null;
}

export interface BrokerConnectionSummary {
  id: string;
  broker: string;
  label: string;
  environment: string;
  status: ConnectionStatus;
  external_user_id: string | null;
  last_sync_at: string | null;
  /** Why the last sync failed. Rendered — a broken connection that looks fine is worse. */
  last_error: string | null;
  created_at: string;
}

export interface BrokerConnectionList {
  items: BrokerConnectionSummary[];
}

export interface SyncResponse {
  connection_id: string;
  executions_ingested: number;
  trades_reconstructed: number;
  deferred: number;
  started_at: string;
  finished_at: string;
}
