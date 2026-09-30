// build.ts inlines any MENTRA_PUBLIC_* env var as a string literal at build
// time (see its `define` step) - there's no Node runtime here, so `process`
// itself never exists on-device. This just satisfies tsc for that pattern.
declare const process: {env: Record<string, string | undefined>}
