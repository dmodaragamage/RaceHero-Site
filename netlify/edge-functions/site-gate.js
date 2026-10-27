// site-gate.js — password gate for racehero.ca until launch.
//
// Founder directive (2026-10-26): competitors are circling, so the whole
// public site sits behind HTTP Basic Auth until the app launches. The
// gate is declared for path "/*" in netlify.toml and this function
// decides per request:
//
//   - /api/* and /.netlify/* are NEVER gated. /api/gemini is the app's AI
//     endpoint (live product traffic) and /api/dev-ingest is the dev
//     funnel — gating either breaks the app, not just the website.
//   - If the SITE_GATE_PASSWORD env var is NOT set, the gate is INERT:
//     every request passes through. Arming is a deliberate founder act —
//     set SITE_GATE_PASSWORD in Netlify (Site settings → Environment
//     variables) and redeploy. Optional SITE_GATE_USER overrides the
//     username (default "racehero"). Setting the env var is the ONLY
//     switch; deleting it (or the edge function) reopens the site.
//   - If armed, requests without valid Basic credentials get a plain
//     401 with a WWW-Authenticate challenge, realm "RaceHero".
//
// Credentials live only in Netlify env vars — never in this repo.

function gateEnv(name) {
  try {
    if (typeof Netlify !== "undefined" && Netlify.env) {
      const v = Netlify.env.get(name);
      if (v != null) return v;
    }
  } catch (_) {
    /* fall through to Deno.env */
  }
  try {
    if (typeof Deno !== "undefined" && Deno.env) {
      return Deno.env.get(name);
    }
  } catch (_) {
    /* env unavailable — treated as unset */
  }
  return undefined;
}

// Constant-time-ish string compare: always walks the longer input and
// folds the length difference into the result, so the loop count and
// branch pattern don't leak where two strings diverge.
function timingSafeEqual(a, b) {
  const ea = new TextEncoder().encode(String(a));
  const eb = new TextEncoder().encode(String(b));
  let diff = ea.length ^ eb.length;
  const n = Math.max(ea.length, eb.length);
  for (let i = 0; i < n; i++) {
    const x = ea.length ? ea[i % ea.length] : 0;
    const y = eb.length ? eb[i % eb.length] : 0;
    diff |= x ^ y;
  }
  return diff === 0;
}

export default async (request, context) => {
  const { pathname } = new URL(request.url);

  // API + Netlify internals: never gated (see header comment).
  if (
    pathname === "/api" ||
    pathname.startsWith("/api/") ||
    pathname === "/.netlify" ||
    pathname.startsWith("/.netlify/")
  ) {
    return context.next();
  }

  const password = gateEnv("SITE_GATE_PASSWORD");
  if (!password) {
    // Gate not armed yet: the site stays open until the founder sets the
    // password. Do NOT fail closed before then.
    return context.next();
  }

  const expectedUser = gateEnv("SITE_GATE_USER") || "racehero";
  const header = request.headers.get("authorization") || "";
  let authorized = false;

  if (header.toLowerCase().startsWith("basic ")) {
    try {
      const bytes = Uint8Array.from(
        atob(header.slice(6).trim()),
        (c) => c.charCodeAt(0),
      );
      const decoded = new TextDecoder().decode(bytes);
      const sep = decoded.indexOf(":");
      if (sep >= 0) {
        // Both compares run before the branch, so a wrong username and
        // a wrong password cost the same work.
        const userOk = timingSafeEqual(decoded.slice(0, sep), expectedUser);
        const passOk = timingSafeEqual(decoded.slice(sep + 1), password);
        authorized = userOk && passOk;
      }
    } catch (_) {
      authorized = false;
    }
  }

  if (authorized) {
    return context.next();
  }

  return new Response("RaceHero — launching soon.", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="RaceHero", charset="UTF-8"',
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
};
