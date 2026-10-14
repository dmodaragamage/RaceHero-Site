# racehero-site

Public website for **RaceHero** — https://racehero.ca

Static site, deployed on Netlify (auto-deploys from `main`).

## What's here

- `index.html` — landing page: features, RaceHero Link section, download CTAs
- `privacy.html` — privacy policy (required for the Google Play listing)
- `netlify.toml` — redirects:
  - `/link` → `RaceHeroLinkSetup.exe` (the PC companion the app points to)
  - `/app` → Google Play (update to the real listing URL at launch)

## Adding the RaceHero Link installer

1. On the Windows PC: run `tools/racehero-link/build-exe.bat`, then `build-installer.bat`
2. Copy `RaceHeroLinkSetup.exe` into this folder (next to `index.html`)
3. Commit & push — Netlify redeploys and `racehero.ca/link` starts serving it.
   No app update needed; the app already points at that URL.

## Deploy

Netlify → Add new site → Import an existing project → select `racehero-site` →
build settings: none (static). Then add the custom domain `racehero.ca`.
DNS is already on Netlify (nameservers at Namecheap point to Netlify DNS).
