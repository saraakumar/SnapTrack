import {useState} from "react"
import {APP_KEY, BACKEND_URL, UPLOAD_TIMEOUT_MS} from "../shared/config"

type Totals = {calories?: number; protein_g?: number; carbs_g?: number; fat_g?: number}
type AnalysisItem = {description: string}
type AnalysisResult = {
  error?: string
  items?: AnalysisItem[]
  totals?: Totals
  full_description?: string
}

function mealTypeForNow(): string {
  const h = new Date().getHours()
  if (h >= 4 && h < 11) return "Breakfast"
  if (h >= 11 && h < 16) return "Lunch"
  if (h >= 16 && h < 22) return "Dinner"
  return "Snack"
}

export function App() {
  const [status, setStatus] = useState<string>("Tap below, then look at your meal.")
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<AnalysisResult | null>(null)

  async function snapAndLog() {
    if (busy) return
    setBusy(true)
    setResult(null)
    setStatus("Capturing (check glasses for shutter sound)...")

    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), UPLOAD_TIMEOUT_MS)

    // Each stage gets its own try/catch with a distinct, step-labeled error
    // message - "Load failed" from a raw fetch doesn't say WHICH fetch threw,
    // and this app has already hit two different fetch failure modes
    // (background JSContext fetch: 200 OK but empty body; CORS block: throws
    // before even reaching a response) so pinning the exact step matters.
    let photoUrl: string, mimeType: string | undefined
    try {
      const photo = await mentra.request("takePhoto", {})
      photoUrl = photo.photoUrl
      mimeType = photo.mimeType
    } catch (e) {
      setStatus(`[capture] ${String(e)}`)
      setBusy(false)
      clearTimeout(timeout)
      return
    }

    let blob: Blob
    try {
      setStatus("Fetching photo...")
      const photoResponse = await fetch(photoUrl, {signal: controller.signal})
      if (!photoResponse.ok) {
        setStatus(`[photo-fetch] status ${photoResponse.status}`)
        setBusy(false)
        clearTimeout(timeout)
        return
      }
      blob = await photoResponse.blob()
      if (blob.size === 0) {
        setStatus("[photo-fetch] got 0 bytes")
        setBusy(false)
        clearTimeout(timeout)
        return
      }
    } catch (e) {
      setStatus(`[photo-fetch] ${String(e)}`)
      setBusy(false)
      clearTimeout(timeout)
      return
    }

    let analysis: AnalysisResult
    try {
      setStatus(`Analyzing (${Math.round(blob.size / 1024)}KB)...\nFirst request after idle can take ~1 min.`)
      const formData = new FormData()
      formData.append("file", blob, `meal.${mimeType?.split("/")[1] || "jpg"}`)
      const uploadResponse = await fetch(`${BACKEND_URL}/upload`, {
        method: "POST",
        headers: {"X-App-Key": APP_KEY},
        body: formData,
        signal: controller.signal,
      })
      analysis = await uploadResponse.json()
    } catch (e) {
      const timedOut = e instanceof Error && e.name === "AbortError"
      setStatus(timedOut ? "[analyze] timed out" : `[analyze] ${String(e)}`)
      setBusy(false)
      clearTimeout(timeout)
      return
    }

    clearTimeout(timeout)

    if (analysis.error) {
      setStatus(`[analyze] server said: ${analysis.error}`)
      setBusy(false)
      return
    }
    if (!analysis.items?.length) {
      setStatus("No food detected. Try a clearer photo.")
      setBusy(false)
      return
    }

    setResult(analysis)
    setStatus("Logging meal...")

    try {
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
      setStatus("Logged!")
    } catch (e) {
      setStatus(`[log] analyzed OK but failed to save: ${String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const totals = result?.totals

  return (
    <div className="app">
      <h1>SnapTrack</h1>

      <button type="button" onClick={() => void snapAndLog()} disabled={busy}>
        {busy ? "Working..." : "📸 Snap & log meal"}
      </button>

      <div className="status-card">{status}</div>

      {totals && (
        <div className="result">
          <div className="result-kcal">{totals.calories != null ? Math.round(totals.calories) : "—"} kcal</div>
          <div className="result-macros">
            {totals.protein_g != null && <span>{Math.round(totals.protein_g)}g protein</span>}
            {totals.carbs_g != null && <span>{Math.round(totals.carbs_g)}g carbs</span>}
            {totals.fat_g != null && <span>{Math.round(totals.fat_g)}g fat</span>}
          </div>
          <ul className="result-items">
            {result?.items?.map((item, i) => (
              <li key={i}>{item.description}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
