/**
 * RaceHero DEV FUNNEL ingest — Netlify Function (v2 format).
 *
 * Founder-only debugging tap for test builds of the app: during test
 * drives the app POSTs session exports + diagnostics here so they can
 * be read back immediately, instead of the manual export→upload loop.
 * Payloads live in Netlify Blobs (store "dev-funnel"). This is NOT a
 * product feature and no page links here.
 *
 * Why v2: the original v1 (CJS) handler crashed at getStore() with
 * MissingBlobsEnvironmentError — functions served in the legacy format
 * never received the Blobs environment. v2 functions (default export +
 * config.path, .mjs) get the Blobs context injected automatically.
 *
 * Auth: every request needs the header  x-dev-token: <DEV_FUNNEL_TOKEN>
 * (Netlify env var). If the env var is unset the endpoint is inert and
 * answers 503 to everything. Env changes need a redeploy.
 *
 *   POST /api/dev-ingest            body = one JSON payload (≤ 8 MB)
 *                                   → 200 {"ok":true,"key":"…"}
 *   GET  /api/dev-ingest?list=1     → newest 50 stored keys + metadata
 *        Optional list filters (v50.14, data study R2):
 *        &kind=<kind>   only keys whose kind segment matches
 *        &limit=<n>     page size (default 50, clamped 1..200)
 *        &before=<key>  keyset paging: only keys strictly less than
 *                       <key> in the same newest-first order
 *        Default-view semantics (R2 fix, 2026-10-27): with NO explicit
 *        &kind=, heartbeat kinds (link, ping) are EXCLUDED — Link posts
 *        a heartbeat roughly every 45 s, and an unfiltered newest-50
 *        window covers only ~35 minutes, which buried whole sessions.
 *        Heartbeats stay fully retrievable via &kind=link / &kind=ping,
 *        and &before= pages arbitrarily far back inside any view, so a
 *        session is always retrievable: ?list=1&kind=session plus
 *        &before=<oldest key of the previous page> until found.
 *   GET  /api/dev-ingest?key=<key>  → the stored payload JSON
 */

import { getStore } from "@netlify/blobs";

const MAX_BYTES = 8 * 1024 * 1024;

function json(status, obj) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Keys are built from payload fields — keep them boring and safe. */
function sanitize(s) {
  return String(s == null ? "" : s)
    .replace(/[^a-zA-Z0-9._-]/g, "")
    .slice(0, 80);
}

/**
 * The kind segment of a stored key. Keys are
 * `<ISO>-<kind>-<sessionId>.json` where the ISO prefix is
 * `receivedAt.replace(/:/g, "-")` — a fixed-shape 24-char timestamp
 * (`2026-10-26T22-38-19.123Z`) — so the kind is the first `-`-
 * delimited segment of what follows it (every kind the app sends —
 * session / lap / diag / ping — is a single segment). Returns null
 * for a key that doesn't carry the prefix (defensive: such a key
 * has no kind segment to match).
 */
function kindSegment(key) {
  const m = /^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.\d{3}Z-([^-]+)-/.exec(key);
  return m ? m[1] : null;
}

export default async (req) => {
  const token =
    (typeof Netlify !== "undefined" && Netlify.env.get("DEV_FUNNEL_TOKEN")) ||
    process.env.DEV_FUNNEL_TOKEN;
  if (!token) {
    return json(503, { error: "dev funnel not configured" });
  }
  const given = req.headers.get("x-dev-token");
  if (!given || given !== token) {
    return json(401, { error: "bad dev token" });
  }

  const store = getStore("dev-funnel");
  const url = new URL(req.url);

  if (req.method === "POST") {
    const raw = await req.text();
    const bytes = Buffer.byteLength(raw, "utf8");
    if (bytes > MAX_BYTES) {
      return json(413, { error: "payload too large" });
    }
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch {
      return json(400, { error: "bad json" });
    }
    const receivedAt = new Date().toISOString();
    const kind = sanitize(payload && payload.kind) || "session";
    const sid = sanitize(payload && payload.sessionId) || "misc";
    const key =
      receivedAt.replace(/:/g, "-") + "-" + kind + "-" + sid + ".json";
    await store.set(key, raw, {
      metadata: { receivedAt, kind, sessionId: sid, bytes },
    });
    return json(200, { ok: true, key });
  }

  if (req.method === "GET") {
    if (url.searchParams.get("list")) {
      // Optional filters (v50.14, data study R2). `kind` goes
      // through the same sanitize() as stored kinds; `before` is
      // used ONLY as a comparison string (never as a store key),
      // so it is taken raw — sanitize() would truncate a long
      // legitimate key at 80 chars and break the keyset.
      const kindFilter = sanitize(url.searchParams.get("kind") || "");
      const rawLimit = parseInt(url.searchParams.get("limit") || "", 10);
      const limit = Number.isFinite(rawLimit)
        ? Math.min(200, Math.max(1, rawLimit))
        : 50;
      const before = url.searchParams.get("before") || "";
      const { blobs } = await store.list();
      let keys = blobs
        .map((b) => b.key)
        .sort()
        .reverse();
      if (kindFilter) {
        keys = keys.filter((k) => kindSegment(k) === kindFilter);
      } else {
        // Default view: heartbeats (Link keepalives, app pings) are
        // excluded so they cannot crowd session/lap/diag payloads out
        // of the window. They remain one explicit &kind= away.
        const HEARTBEAT_KINDS = new Set(["link", "ping"]);
        keys = keys.filter((k) => !HEARTBEAT_KINDS.has(kindSegment(k)));
      }
      if (before) {
        // Same descending order as the listing itself: strictly
        // less than the given key (string order on the ISO-
        // prefixed keys is chronological).
        keys = keys.filter((k) => k < before);
      }
      keys = keys.slice(0, limit);
      const items = [];
      for (const key of keys) {
        let meta = null;
        try {
          const m = await store.getMetadata(key);
          meta = (m && m.metadata) || null;
        } catch {
          meta = null;
        }
        items.push({
          key,
          receivedAt: meta ? meta.receivedAt || null : null,
          bytes: meta ? meta.bytes ?? null : null,
          kind: meta ? meta.kind || null : null,
          sessionId: meta ? meta.sessionId || null : null,
        });
      }
      return json(200, { ok: true, items });
    }
    const keyParam = url.searchParams.get("key");
    if (keyParam) {
      if (keyParam.includes("/") || keyParam.includes("..")) {
        return json(400, { error: "bad key" });
      }
      const data = await store.get(keyParam, { type: "text" });
      if (data == null) {
        return json(404, { error: "not found" });
      }
      return new Response(data, {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    return json(400, { error: "use ?list=1 or ?key=<key>" });
  }

  return json(405, { error: "GET or POST only" });
};

export const config = { path: "/api/dev-ingest" };
