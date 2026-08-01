"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { PageHeader } from "@/components/shell";
import { ApiError, request } from "@/lib/api";
import { formatDate } from "@/lib/format";
import type {
  BrokerConnectionList,
  BrokerConnectionSummary,
  ConnectBrokerRequest,
  ConnectionStatus,
  SyncResponse,
} from "@/types/broker";

/**
 * Linking a broker.
 *
 * This is the only screen in the application that asks for a credential, so the rules it
 * follows are worth stating rather than leaving to be inferred from the markup.
 *
 * **The form holds credentials in component state and nothing else.** They are not put in
 * a query cache, not in `localStorage`, not in a URL, and not in a Zustand store. The
 * mutation sends them and the state is cleared on success, so a successful link leaves no
 * copy behind in the tab.
 *
 * **`autoComplete="off"` on the API key fields, `new-password` on the password.** A
 * browser password manager offering to save a Tradovate API secret alongside website
 * logins is a place credentials end up that nobody chose.
 *
 * **Demo is the default environment.** Tradovate's simulation environment exercises the
 * identical code path against the real API, and the first thing anyone should do with an
 * integration that has never touched production is point it somewhere that cannot lose
 * money. Choosing `live` is a deliberate act, and the form says what it means.
 *
 * **The connection list renders `last_error`.** A broker link that silently stopped
 * syncing is the failure this whole product cannot tolerate — the promise is that trades
 * arrive without being journalled by hand, and a connection that looks healthy while
 * importing nothing breaks that promise invisibly.
 */

const STATUS_TONE: Record<ConnectionStatus, string> = {
  active: "border-emerald-500/40 text-emerald-300",
  error: "border-rose-500/40 text-rose-300",
  revoked: "border-slate-600 text-slate-400",
  expired: "border-amber-500/40 text-amber-300",
};

const EMPTY: ConnectBrokerRequest = {
  broker: "tradovate",
  label: "",
  environment: "demo",
  username: "",
  password: "",
  cid: "",
  secret: "",
};

function Field({
  label,
  value,
  onChange,
  type = "text",
  hint,
  autoComplete = "off",
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: string;
  hint?: string;
  autoComplete?: string;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[11px] uppercase tracking-wider text-slate-400">
        {label}
      </span>
      <input
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        autoComplete={autoComplete}
        spellCheck={false}
        className="rounded border border-slate-700 bg-slate-900 px-3 py-2 font-mono text-sm text-slate-200 focus:border-slate-500 focus:outline-none"
      />
      {hint ? <span className="text-[11px] text-slate-600">{hint}</span> : null}
    </label>
  );
}

function Connection({ connection }: { connection: BrokerConnectionSummary }) {
  const client = useQueryClient();
  const sync = useMutation({
    mutationFn: () =>
      request<SyncResponse>(`/broker/connections/${connection.id}/sync`, {
        method: "POST",
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["broker", "connections"] });
      void client.invalidateQueries({ queryKey: ["trades"] });
    },
  });

  return (
    <article className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
      <header className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-medium text-slate-200">{connection.label}</h3>
          <p className="mt-0.5 font-mono text-[11px] text-slate-500">
            {connection.broker} · {connection.environment}
            {connection.external_user_id ? ` · #${connection.external_user_id}` : ""}
          </p>
        </div>
        <span
          className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${
            STATUS_TONE[connection.status] ?? STATUS_TONE.revoked
          }`}
        >
          {connection.status}
        </span>
      </header>

      <dl className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500">
        <div className="flex gap-1">
          <dt>linked</dt>
          <dd className="font-mono text-slate-400">
            {formatDate(connection.created_at)}
          </dd>
        </div>
        <div className="flex gap-1">
          <dt>last sync</dt>
          {/* "never" rather than a blank. A connection that has never synced looks
              identical to one that syncs fine until you notice the gap. */}
          <dd className="font-mono text-slate-400">
            {connection.last_sync_at ? formatDate(connection.last_sync_at) : "never"}
          </dd>
        </div>
      </dl>

      {connection.last_error ? (
        <p className="mt-3 rounded border border-rose-500/30 bg-rose-500/5 p-2 font-mono text-[11px] text-rose-200">
          {connection.last_error}
        </p>
      ) : null}

      <div className="mt-3 flex items-center gap-3">
        <button
          type="button"
          onClick={() => sync.mutate()}
          disabled={sync.isPending}
          className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-50"
        >
          {sync.isPending ? "Syncing…" : "Sync now"}
        </button>
        {sync.data ? (
          <span className="font-mono text-[11px] text-slate-400">
            {sync.data.executions_ingested} fills ·{" "}
            {sync.data.trades_reconstructed} trades
            {sync.data.deferred > 0 ? ` · ${sync.data.deferred} deferred` : ""}
          </span>
        ) : null}
        {sync.isError ? (
          <span className="text-[11px] text-rose-300">
            {sync.error instanceof ApiError ? sync.error.message : "Sync failed."}
          </span>
        ) : null}
      </div>
    </article>
  );
}

export default function BrokerPage() {
  const client = useQueryClient();
  const [form, setForm] = useState<ConnectBrokerRequest>(EMPTY);

  const connections = useQuery({
    queryKey: ["broker", "connections"],
    queryFn: () => request<BrokerConnectionList>("/broker/connections"),
  });

  const connect = useMutation({
    mutationFn: (payload: ConnectBrokerRequest) =>
      request<BrokerConnectionSummary>("/broker/connections", {
        method: "POST",
        body: payload,
      }),
    onSuccess: () => {
      // Cleared immediately: a successful link should leave no copy of the credential in
      // this tab's memory for the rest of the session.
      setForm(EMPTY);
      void client.invalidateQueries({ queryKey: ["broker", "connections"] });
    },
  });

  const set = (key: keyof ConnectBrokerRequest) => (value: string) =>
    setForm((current) => ({ ...current, [key]: value }));

  const complete =
    form.label && form.username && form.password && form.cid && form.secret;

  return (
    <>
      <PageHeader
        title="Broker"
        subtitle="Link a login once; every fill after that arrives on its own."
      />

      <div className="space-y-6 p-6">
        <section className="max-w-3xl rounded-lg border border-slate-800 bg-slate-900/40 p-5">
          <h2 className="text-xs font-medium uppercase tracking-wider text-slate-400">
            Link a Tradovate account
          </h2>
          <p className="mt-1 text-[11px] leading-relaxed text-slate-500">
            Credentials are verified against Tradovate before anything is stored — if the
            login does not work you get an error and no connection is created. They are
            then written to the secret store encrypted; the database holds a reference,
            never the password, and no endpoint returns them.
          </p>

          <form
            className="mt-4 grid gap-4 sm:grid-cols-2"
            onSubmit={(event) => {
              event.preventDefault();
              connect.mutate(form);
            }}
          >
            <Field
              label="Label"
              value={form.label}
              onChange={set("label")}
              hint="Yours, for telling accounts apart"
            />

            <label className="flex flex-col gap-1">
              <span className="text-[11px] uppercase tracking-wider text-slate-400">
                Environment
              </span>
              <select
                value={form.environment}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    environment: event.target.value as "demo" | "live",
                  }))
                }
                className="rounded border border-slate-700 bg-slate-900 px-3 py-2 font-mono text-sm text-slate-200 focus:border-slate-500 focus:outline-none"
              >
                <option value="demo">demo — simulation</option>
                <option value="live">live — real money</option>
              </select>
              <span
                className={`text-[11px] ${
                  form.environment === "live" ? "text-amber-400/80" : "text-slate-600"
                }`}
              >
                {form.environment === "live"
                  ? "Reads a funded account. Start on demo if this integration is new to you."
                  : "Same code path, same API, no money at risk."}
              </span>
            </label>

            <Field
              label="Username"
              value={form.username}
              onChange={set("username")}
              autoComplete="username"
            />
            <Field
              label="Password"
              value={form.password}
              onChange={set("password")}
              type="password"
              // `new-password` rather than `current-password`: it suppresses the
              // browser's offer to autofill — and, more to the point, to *save* — a
              // broker credential in a website password store.
              autoComplete="new-password"
            />
            <Field
              label="API key (cid)"
              value={form.cid}
              onChange={set("cid")}
              hint="Tradovate Trader → Application Settings → API Access"
            />
            <Field
              label="API secret"
              value={form.secret}
              onChange={set("secret")}
              type="password"
              autoComplete="new-password"
            />

            <div className="sm:col-span-2 flex items-center gap-4">
              <button
                type="submit"
                disabled={!complete || connect.isPending}
                className="rounded border border-slate-700 bg-slate-800 px-4 py-2 text-sm text-slate-200 hover:bg-slate-700 disabled:opacity-50"
              >
                {connect.isPending ? "Verifying…" : "Verify and link"}
              </button>
              {connect.isError ? (
                <span className="text-sm text-rose-300">
                  {connect.error instanceof ApiError
                    ? connect.error.message
                    : "Could not link the account."}
                </span>
              ) : null}
              {connect.isSuccess ? (
                <span className="text-sm text-emerald-300">
                  Linked. Sync it below to import your fills.
                </span>
              ) : null}
            </div>
          </form>
        </section>

        <section>
          <h2 className="mb-3 text-xs font-medium uppercase tracking-wider text-slate-400">
            Linked accounts ({connections.data?.items.length ?? 0})
          </h2>

          {connections.data && connections.data.items.length === 0 ? (
            <p className="max-w-3xl text-sm text-slate-500">
              Nothing linked yet. Until a broker is connected, every screen in this
              application is describing an empty history — the journal does not accept
              trades typed in by hand.
            </p>
          ) : null}

          <div className="grid gap-3 lg:grid-cols-2">
            {connections.data?.items.map((connection) => (
              <Connection key={connection.id} connection={connection} />
            ))}
          </div>
        </section>
      </div>
    </>
  );
}
