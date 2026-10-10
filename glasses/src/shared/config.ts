/**
 * Single source of truth for constants shared by both the background
 * JSContext and the UI WebView bundles. build.ts inlines MENTRA_PUBLIC_*
 * env vars into both at build time.
 */

// Point this at your deployed SnapTrack backend.
export const BACKEND_URL = "https://snaptrack-td9s.onrender.com"

// X-App-Key authenticates machine clients (see app.py's require_access /
// load_current_user). Two things can go here:
//   - your personal API token from /account once you've signed up -
//     captures log to YOUR account, isolated from other users (preferred)
//   - the shared site-wide APP_PASSWORD, if set - works but doesn't
//     identify you as a specific user, so captures land in the pre-account
//     "anonymous" bucket instead of your own history
// Read from a MENTRA_PUBLIC_* env var inlined at build time - copy
// .env.example to .env and fill in the real value. Never hardcode the real
// value here, since this repo is public.
export const APP_KEY = process.env.MENTRA_PUBLIC_APP_KEY ?? ""

// Render's free tier spins down when idle (~50s cold start) - give requests
// enough room to survive that plus the Gemini analysis call itself.
export const UPLOAD_TIMEOUT_MS = 75_000
