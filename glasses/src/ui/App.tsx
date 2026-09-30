import {useEffect, useState} from "react"

export function App() {
  const [roundtripMs, setRoundtripMs] = useState<number | null>(null)

  useEffect(() => {
    const unsub = mentra.on("pong", ({roundtripMs}) => {
      setRoundtripMs(roundtripMs)
    })
    return unsub
  }, [])

  const ping = () => {
    mentra.send("ping", {at: Date.now()})
  }

  return (
    <div className="app">
      <h1>SnapTrack</h1>
      <p>Press the glasses button to snap and log a meal.</p>
      <p>Round-trip to background: {roundtripMs == null ? "—" : `${roundtripMs} ms`}</p>
      <button type="button" onClick={ping}>
        Ping background
      </button>
    </div>
  )
}
