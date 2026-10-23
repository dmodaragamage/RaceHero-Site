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
      const { blobs } = await store.list();
      const keys = blobs
        .map((b) => b.key)
        .sort()
        .reverse()
        .slice(0, 50);
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
