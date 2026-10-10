/**
 * Typed channel registry — single source of truth for the names + payload
 * shapes that flow between this miniapp's background JSContext and its UI
 * WebView. Both halves import this file at build time; the bundler inlines
 * the declarations so there's no runtime resolution.
 *
 * Add a key per channel. The TypeScript generic on `mentra.send` /
 * `mentra.on` / `session.ui.send` / `session.ui.on` enforces names + payload
 * shapes at compile time so the two halves can't drift.
 */

import type {Rpc} from "@mentra/miniapp/background"

export interface Channels {
  // WebView → background
  "ping": {at: number}

  // background → WebView
  "pong": {at: number; roundtripMs: number}

  // WebView -> background RPC: capture a photo via the glasses camera
  // (hardware control must stay in background) and return its download URL
  // for the WebView's own real fetch/Blob-capable network stack to fetch -
  // the background JSContext's fetch() can't reliably read large
  // cross-origin binary bodies on this hardware (see glasses/README.md).
  "takePhoto": Rpc<Record<string, never>, {photoUrl: string; mimeType: string}>
}

declare global {
  // Augment the `mentra` global from @mentra/miniapp/ui with this miniapp's
  // typed channel registry so authors get compile-time enforcement on every
  // mentra.send / mentra.on call without re-declaring it.
  // eslint-disable-next-line no-var
  var mentra: import("@mentra/miniapp/ui").MentraTyped<Channels>
}
