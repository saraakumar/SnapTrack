# SnapTrack for Mentra glasses

A MentraOS miniapp (`@mentra/miniapp`) that gives SnapTrack a hands-free
capture loop: press the glasses button, get calories and macros on the HUD,
with the meal already logged by the time the text appears.

## How it works

All the logic lives in `src/background/index.ts`, which runs on-device in the
background JSContext (always alive while MentraOS is running, independent of
whether the WebView UI is open):

1. `session.input.onButtonPress` triggers the flow - no voice/wake-word API
   exists in the current SDK, so the physical button is the trigger.
2. `session.camera.takePhoto()` captures the meal photo.
3. The photo bytes are fetched and base64-encoded, then POSTed as JSON to the
   existing Flask backend's `/upload` route.
4. The analysis response is rendered on the HUD (`session.display.render()`)
   and simultaneously logged via `/api/meals` - zero taps end to end.

**Why base64 JSON instead of multipart/form-data:** the on-device JS runtime
(JavaScriptCore on iOS, QuickJS on Android) has `fetch()` but no `FormData`
or `Blob` - see `two-layer-architecture.md` in the MentraOS docs. `app.py`'s
`/upload` route accepts either a multipart file (used by the web/mobile UI)
or a JSON body `{"image_base64": ..., "mime_type": ...}` (used here), so the
Flask side needed no new endpoint.

**Auth:** the miniapp sends the same `X-App-Key` header the web app's server
already checks (`APP_PASSWORD` in `app.py`). This is a single-user hobby
project - if you ever distribute this miniapp beyond your own glasses, put
that key behind a real per-user login instead of a hardcoded constant in
`background/index.ts`.

**Render cold starts:** the free-tier backend spins down when idle (~50s
wake). The upload has a 75s timeout and the HUD shows a "this can take ~1
min" message so a cold start doesn't look like a hang.

## Setup

```bash
cd glasses
bun install
```

Edit the two constants at the top of `src/background/index.ts` if your
deployment URL or app password ever change.

## Commands

```bash
bun run typecheck   # tsc --noEmit
bun run build       # bundles to dist/background and dist/ui
bun dev             # MentraOS dev server (requires the Mentra CLI/device pairing)
```

## Known rough edges in the current SDK (Sept 2026)

- `create-mentra-miniapp` (the official scaffolder) hangs when run with
  non-interactive stdin - its `@clack/prompts` UI spins instead of reading
  EOF. Pass `--type camera <name>` and it *should* skip prompts, but the
  process still spun for us; we ended up copying the package's `template/`
  directory by hand. If re-scaffolding, try running it in a real terminal
  first.
- The scaffolder pins `@mentra/miniapp@0.3.0-dev.0` even though the current
  published `latest` is a 3.x line - the SDK went through a major
  renumbering. Stick with the pinned dev version unless you've confirmed the
  3.x line still matches this scaffold's template/CLI.
