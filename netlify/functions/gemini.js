/**
 * RaceHero AI proxy — Netlify Function.
 *
 * Holds the Gemini API key server-side (Netlify env var GEMINI_API_KEY)
 * so no build of the app ever contains one. Customers set up nothing:
 * the app calls https://racehero.ca/api/gemini and it just works.
 *
 * Two request shapes:
 *  - {"prompt": "..."}            → debrief; runs the model fallback
 *    chain here and returns {"text": "..."}.
 *  - {"model": "gemini-…", "payload": {…}} → raw generateContent
 *    passthrough (AI voice TTS); returns Google's status + body as-is.
 *
 * Env changes need a redeploy before functions pick them up.
 */

const MODELS = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-2.5-flash"];
const MODEL_RE = /^gemini-[a-z0-9.\-]+$/;

function json(statusCode, obj) {
  return {
    statusCode,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(obj),
  };
}

async function callGemini(key, model, payload) {
  const res = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${key}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }
  );
  return { status: res.status, text: await res.text() };
}

function extractText(data) {
  const cand = data?.candidates?.[0];
  const parts = cand?.content?.parts || [];
  for (const p of parts) {
    if (p.thought) continue;
    if (typeof p.text === "string" && p.text.trim()) return p.text;
  }
  return "";
}

exports.handler = async (event) => {
  if (event.httpMethod !== "POST") {
    return json(405, { error: "POST only" });
  }
  const key = process.env.GEMINI_API_KEY;
  if (!key) {
    return json(500, { error: "proxy not configured" });
  }
  let body;
  try {
    body = JSON.parse(event.body || "{}");
  } catch {
    return json(400, { error: "bad json" });
  }

  try {
    // Shape B: raw passthrough (voice TTS).
    if (typeof body.model === "string" && body.payload) {
      if (!MODEL_RE.test(body.model)) {
        return json(400, { error: "bad model" });
      }
      const up = await callGemini(key, body.model, body.payload);
      return {
        statusCode: up.status,
        headers: { "Content-Type": "application/json" },
        body: up.text,
      };
    }

    // Shape A: prompt → text, with the model fallback chain server-side
    // so a Google model retirement degrades instead of breaking debriefs.
    if (typeof body.prompt === "string" && body.prompt.length > 0) {
      if (body.prompt.length > 20000) {
        return json(400, { error: "prompt too long" });
      }
      const payload = {
        contents: [{ parts: [{ text: body.prompt }] }],
      };
      let last = null;
      for (const model of MODELS) {
        const up = await callGemini(key, model, payload);
        if (up.status === 404) {
          last = up;
          continue; // model retired for this key — try the next
        }
        if (up.status !== 200) {
          return json(502, {
            error: `gemini ${up.status}`,
            detail: up.text.slice(0, 300),
          });
        }
        const text = extractText(JSON.parse(up.text));
        if (!text) {
          return json(502, { error: "empty reply from AI" });
        }
        return json(200, { text });
      }
      return json(502, {
        error: "no AI model available",
        detail: (last?.text || "").slice(0, 300),
      });
    }

    return json(400, { error: "expected {prompt} or {model, payload}" });
  } catch (e) {
    return json(502, { error: "proxy failure", detail: String(e).slice(0, 200) });
  }
};
