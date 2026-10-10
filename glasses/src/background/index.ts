/**
 * Background JSContext entry point. Runs the whole hands-free loop: button
 * press -> capture -> analyze -> auto-log -> show totals on the HUD.
 *
 * The on-device runtime (JavaScriptCore/QuickJS, not Node or a browser) has
 * fetch() but no FormData/Blob - see two-layer-architecture.md. So the photo
 * goes to the backend as a JSON body with a base64 image instead of
 * multipart/form-data; app.py's /upload route accepts either.
 */

import {registerMiniapp} from "@mentra/miniapp/background"
import "../shared/channels"
import {APP_KEY, BACKEND_URL, UPLOAD_TIMEOUT_MS} from "../shared/config"

function mealTypeForNow(): string {
  const h = new Date().getHours()
  if (h >= 4 && h < 11) return "Breakfast"
  if (h >= 11 && h < 16) return "Lunch"
  if (h >= 16 && h < 22) return "Dinner"
  return "Snack"
}

// btoa(String.fromCharCode(...bytes)) blows the call stack on anything but
// tiny arrays; chunk it the way large-typed-array base64 encoding usually is.
function toBase64(bytes: Uint8Array): string {
  const CHUNK = 0x8000
  let binary = ""
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK))
  }
  return btoa(binary)
}

registerMiniapp(async (session) => {
  // Diagnostics for the real-hardware button trigger: log what this device
  // actually reports, since different glasses models wire input differently.
  console.log("[snaptrack] session ready, capabilities:", JSON.stringify(session.capabilities))

  function showText(text: string) {
    console.log("[snaptrack] display:", text.replace(/\n/g, " / "))
    const d = session.capabilities?.display
    session.display
      .render([
        {
          type: "text",
          id: "status",
          box: {x: 0, y: 0, w: d?.width ?? 576, h: d?.height ?? 288},
          text,
        },
      ])
      .catch((e) => console.error("[snaptrack] display.render failed:", e))
  }

  // Multiple presses/taps in quick succession would otherwise spawn
  // overlapping captures that race each other on the display - ignore new
  // triggers until the current one finishes.
  let busy = false

  async function captureAndLog() {
    if (busy) {
      console.log("[snaptrack] capture already in progress, ignoring trigger")
      return
    }
    busy = true
    try {
      await runCapture()
    } finally {
      busy = false
    }
  }

  async function runCapture() {
    showText("Snapping photo...")

    let photo: {photoUrl: string; mimeType?: string}
    try {
      // "low" size as a first test: if a smaller binary body gets through
      // where "medium" didn't, that confirms a payload-size limit in this
      // runtime's fetch rather than a per-request auth/CORS failure.
      photo = await session.camera.takePhoto({size: "low"})
      console.log("[snaptrack] photo captured, photoUrl:", photo.photoUrl, "mimeType:", photo.mimeType)
    } catch (e) {
      console.error("[snaptrack] takePhoto failed:", e)
      showText("Camera failed - try again")
      return
    }

    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), UPLOAD_TIMEOUT_MS)

    try {
      // No AbortSignal on this GET: testing whether combining signal with a
      // large cross-origin binary body read is what's zeroing out the bytes
      // in this runtime's fetch polyfill (headers parse fine either way).
      const photoResponse = await fetch(photo.photoUrl)
      const bytes = new Uint8Array(await photoResponse.arrayBuffer())

      if (!photoResponse.ok || bytes.length === 0) {
        // Shown on the HUD (not just console.log) since the dev log pipe
        // has been silently dropping some diagnostic lines over BLE/Wi-Fi.
        const len = photoResponse.headers.get("content-length") ?? "?"
        const type = photoResponse.headers.get("content-type") ?? "?"
        showText(`Photo transfer failed\nstatus ${photoResponse.status}, got ${bytes.length}b\nheader len=${len} type=${type}`)
        return
      }

      const b64 = toBase64(bytes)
      showText(`Analyzing (${bytes.length}b -> ${b64.length}b64)...\n(first request after idle can take ~1 min)`)

      const analysis = await fetch(`${BACKEND_URL}/upload`, {
        method: "POST",
        headers: {"X-App-Key": APP_KEY, "Content-Type": "application/json"},
        body: JSON.stringify({
          image_base64: b64,
          mime_type: photo.mimeType || "image/jpeg",
        }),
        signal: controller.signal,
      }).then((r) => r.json())

      if (analysis.error) {
        showText(`Analysis failed:\n${analysis.error}`)
        return
      }
      if (!analysis.items?.length) {
        showText("No food detected.\nTry a clearer photo.")
        return
      }

      const totals = analysis.totals || {}
      const headline = totals.calories != null ? `${Math.round(totals.calories)} kcal` : "Logged"
      const macros = [
        totals.protein_g != null ? `${Math.round(totals.protein_g)}p` : null,
        totals.carbs_g != null ? `${Math.round(totals.carbs_g)}c` : null,
        totals.fat_g != null ? `${Math.round(totals.fat_g)}f` : null,
      ]
        .filter(Boolean)
        .join(" · ")
      const topItem = analysis.items[0]?.description ?? ""

      showText([headline, macros, topItem].filter(Boolean).join("\n"))

      // Auto-log so the hands-free loop takes zero taps; the phone app can
      // still adjust portions or delete the entry afterward.
      await fetch(`${BACKEND_URL}/api/meals`, {
        method: "POST",
        headers: {"X-App-Key": APP_KEY, "Content-Type": "application/json"},
        body: JSON.stringify({
          items: analysis.items,
          totals: analysis.totals,
          summary: analysis.full_description,
          meal_type: mealTypeForNow(),
        }),
      })
    } catch (e) {
      console.error("[snaptrack] upload/analyze failed:", e)
      const timedOut = e instanceof Error && e.name === "AbortError"
      showText(timedOut ? "Timed out - check connection\nand try again" : "Something went wrong")
    } finally {
      clearTimeout(timeout)
    }
  }

  if (!APP_KEY) {
    showText("Setup needed:\nset MENTRA_PUBLIC_APP_KEY\nin glasses/.env, then rebuild")
    return
  }

  showText("Press the button\nor tap the touchpad\nto snap a meal")

  session.input.onButtonPress((press) => {
    console.log("[snaptrack] button press:", JSON.stringify(press))
    void captureAndLog()
  })

  // Fallback trigger: some glasses route their physical control through
  // touch gestures rather than a generic button-press event. Wiring both
  // means whichever this hardware actually emits, capture still fires.
  session.input.onTouch((gesture) => {
    console.log("[snaptrack] touch gesture:", JSON.stringify(gesture))
    void captureAndLog()
  })

  // RPC for the UI WebView: camera control is hardware-gated and must run
  // here in background, but the WebView's real browser fetch/Blob can read
  // the resulting photoUrl's binary body where this JSContext's fetch
  // cannot (see captureAndLog's photoResponse handling above). The UI calls
  // this, then does its own fetch+upload+display using the returned URL.
  //
  // `session.ui` isn't generic over our Channels type the way the UI-side
  // `mentra` global is (MiniappSession takes no type parameter), so `handle`
  // can't infer "takePhoto" as a valid RPC channel here - cast around it;
  // the UI side (mentra.request) keeps full type safety via channels.ts.
  type TakePhotoHandle = (
    channel: "takePhoto",
    handler: () => Promise<{photoUrl: string; mimeType: string}>,
  ) => void
  ;(session.ui.handle as unknown as TakePhotoHandle)("takePhoto", async () => {
    console.log("[snaptrack] UI requested takePhoto")
    const photo = await session.camera.takePhoto({size: "medium"})
    return {photoUrl: photo.photoUrl, mimeType: photo.mimeType}
  })
})
