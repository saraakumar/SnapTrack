/**
 * Single source of truth for constants shared by both the background
 * JSContext and the UI WebView bundles. build.ts inlines MENTRA_PUBLIC_*
 * env vars into both at build time.
 */

// Point this at your deployed SnapTrack backend.
export const BACKEND_URL = "https://snaptrack-td9s.onrender.com"

// X-App-Key authenticates machine clients the same way the mobile web app's
// fetches do (see app.py's require_access before_request hook). Read from a
// MENTRA_PUBLIC_* env var inlined at build time - copy .env.example to .env
// and fill in the real value. Never hardcode the real password here, since
// this repo is public.
export const APP_KEY = process.env.MENTRA_PUBLIC_APP_KEY ?? ""

// Render's free tier spins down when idle (~50s cold start) - give requests
// enough room to survive that plus the Gemini analysis call itself.
export const UPLOAD_TIMEOUT_MS = 75_000
