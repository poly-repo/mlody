# SPEC: mlody Stage — Browser REPL UI

**Version:** 1.0 **Date:** 2026-05-03 **Status:** Approved **Scope:**
`mlody/stage/` — initial UI shell, no real backend

---

## Executive Summary

mlody Stage is a browser-based REPL for the mlody ML pipeline framework. Users
type commands (e.g. `show @lexica//models:bert`) into an input box at the bottom
of the screen, press Enter, and see output rendered progressively in a
scrollable pane above. The first version is a pure UI shell: commands are echoed
back rather than executed against a real backend. Backend integration (WebSocket
to the mlody LSP / executor) slots in later without changing the component
architecture.

**How this maps to requirements:**

| Requirement                                | Satisfied by                                                   |
| ------------------------------------------ | -------------------------------------------------------------- |
| Text input, Enter to execute               | `InputBar` component                                           |
| Input grows vertically, anchored at bottom | CSS `max-height` + `overflow-y: auto` on textarea              |
| Executing blocks with progressive output   | `ExecutionBlock` component + `ExecutionRecord` state           |
| Multiple past executions, scrollable       | `OutputPane` rendering a bounded list of `ExecutionRecord`     |
| Settings placeholder behind gear icon      | `SettingsPage` route                                           |
| Design for structured command / targets    | `InputBar` props contract forwards to future `StructuredInput` |
| Bazel build + dev server                   | `BUILD.bazel` following smoketest pattern                      |

---

## Architecture Overview

```
mlody/stage/
├── BUILD.bazel
├── index.html
├── serve.sh
├── tsconfig.json
└── src/
    ├── main.tsx              entry point
    ├── App.tsx               root: router, layout shell
    ├── types.ts              shared TypeScript types
    ├── executor.ts           command execution abstraction (stub)
    ├── components/
    │   ├── Layout.tsx        full-height flex column
    │   ├── OutputPane.tsx    scrollable list of ExecutionBlock
    │   ├── ExecutionBlock.tsx one past or in-progress execution
    │   ├── InputBar.tsx      auto-growing textarea at bottom
    │   └── GearIcon.tsx      SVG icon, navigates to /settings
    └── pages/
        ├── ReplPage.tsx      assembles Layout + OutputPane + InputBar
        └── SettingsPage.tsx  placeholder settings route
```

### Component tree

```
App
├── ReplPage          (route "/")
│   ├── Layout
│   │   ├── OutputPane
│   │   │   └── ExecutionBlock[]
│   │   └── InputBar
│   └── GearIcon      (floated top-right, navigates to /settings)
└── SettingsPage      (route "/settings")
```

### Data flow

```
User types text → InputBar local state (value)
User presses Enter →
  InputBar calls onSubmit(text) →
    ReplPage.handleSubmit(text):
      1. Creates a new ExecutionRecord { id, command, status: "running", output: [] }
      2. Appends it to executions state
      3. Calls executor.run(text, (chunk) => appendOutput(id, chunk))
      4. executor stub emits one "echo" chunk then resolves
      5. Status transitions: "running" → "done" | "error"
    OutputPane re-renders with updated executions list
    ExecutionBlock for the active record shows incremental output
```

---

## Technical Stack

| Layer           | Choice                                   | Rationale                                                 |
| --------------- | ---------------------------------------- | --------------------------------------------------------- |
| Language        | TypeScript 5.8                           | Already in root `package.json`                            |
| UI library      | React 19                                 | Already in root `package.json`                            |
| Routing         | React Router v7 (`react-router-dom`)     | Industry standard, hash-based router avoids server config |
| Styling         | Plain CSS Modules (`.module.css`)        | Zero extra deps; co-located with components               |
| Build           | `ts_project` + `esbuild_bundle`          | Matches smoketest pattern exactly                         |
| Dev server      | `python3 -m http.server` via `sh_binary` | Matches smoketest pattern                                 |
| Package manager | pnpm (workspace root)                    | Already in use                                            |

No additional npm packages beyond what is already in the root `package.json`
except `react-router-dom` (and its types). That package must be added to the
root `package.json` and `pnpm-lock.yaml` before building.

---

## Shared TypeScript Types (`src/types.ts`)

```typescript
/** One line / chunk of output from an execution. */
export interface OutputChunk {
  text: string;
  /** "stdout" for normal output, "stderr" for errors, "meta" for UI messages */
  kind: "stdout" | "stderr" | "meta";
}

/** Represents one submitted command and its execution state. */
export interface ExecutionRecord {
  id: string; // browser-safe unique id
  command: string;
  /** ISO timestamp when the command was submitted */
  submittedAt: string;
  status: "running" | "done" | "error";
  output: OutputChunk[];
}

/** Callback type used by the executor to stream output chunks */
export type OutputCallback = (chunk: OutputChunk) => void;

/** The executor abstraction — swap stub for real backend without touching UI */
export interface Executor {
  run(command: string, onChunk: OutputCallback): Promise<void>;
}
```

---

## Detailed Component Specifications

### `src/types.ts`

Defines the types above. No runtime code.

---

### `src/executor.ts`

Purpose: decouple the UI from the backend. The stub echoes the command back.
When real backend integration arrives (WebSocket to `mlody/lsp/` or a dedicated
execution service), only this file changes.

```typescript
import type { Executor, OutputCallback } from "./types.js";

export const stubExecutor: Executor = {
  async run(command: string, onChunk: OutputCallback): Promise<void> {
    onChunk({ text: `> ${command}`, kind: "meta" });
    // Simulate async work
    await new Promise((resolve) => setTimeout(resolve, 120));
    onChunk({ text: command, kind: "stdout" });
  },
};
```

The `Executor` interface is defined in `types.ts`. `ReplPage` receives an
`Executor` as a prop (default: `stubExecutor`) so tests can inject a mock.

---

### `src/main.tsx`

Standard React entry point. Wraps `<App>` in `<BrowserRouter>`.

```typescript
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App.js";

createRoot(document.getElementById("root")!).render(
  <BrowserRouter>
    <App />
  </BrowserRouter>
);
```

---

### `src/App.tsx`

Root component. Declares routes. No state.

```typescript
import { Routes, Route } from "react-router-dom";
import { ReplPage } from "./pages/ReplPage.js";
import { SettingsPage } from "./pages/SettingsPage.js";

export function App() {
  return (
    <Routes>
      <Route path="/" element={<ReplPage />} />
      <Route path="/settings" element={<SettingsPage />} />
    </Routes>
  );
}
```

---

### `src/pages/ReplPage.tsx`

The main page. Owns all application state. Renders `Layout`, `OutputPane`,
`InputBar`, and `GearIcon`.

**State:**

```typescript
const [executions, setExecutions] = useState<ExecutionRecord[]>([]);
```

**`handleSubmit(command: string): void`** — the central event handler:

1. Guard: if `command.trim() === ""`, return immediately.
2. Build a new `ExecutionRecord` with `status: "running"` and empty `output`.
3. Call `setExecutions((prev) => [...prev, record].slice(-MAX_EXECUTIONS))`.
4. Call `executor.run(command, (chunk) => { ... })` to append chunks:
   ```typescript
   setExecutions((prev) =>
     prev.map((r) =>
       r.id === record.id ? { ...r, output: [...r.output, chunk] } : r,
     ),
   );
   ```
5. On resolution, set `status: "done"`; on rejection, set `status: "error"` and
   push a stderr chunk with the error message.

**Props:**

```typescript
interface ReplPageProps {
  executor?: Executor; // defaults to stubExecutor
}
```

**Layout:** renders `<Layout>` containing `<OutputPane>` (flex-grow: 1) and
`<InputBar>` (pinned at bottom). `<GearIcon>` is positioned absolutely at
top-right via CSS.

---

### `src/components/Layout.tsx`

A full-viewport-height flex column with no margins or padding. Hosts
`OutputPane` (growing) and `InputBar` (shrinking to content, max-height capped).

```tsx
export function Layout({ children }: { children: React.ReactNode }) {
  return <div className={styles.layout}>{children}</div>;
}
```

CSS (`.module.css`):

```css
.layout {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
  background: #1a1a2e;
  color: #e0e0e0;
  font-family: "JetBrains Mono", "Fira Code", monospace;
  font-size: 14px;
}
```

---

### `src/components/OutputPane.tsx`

Renders a scrollable list of `ExecutionBlock` components, one per record.
Automatically scrolls to the bottom whenever a new execution record is added
(not on every chunk, to avoid fighting user scroll).

**Key implementation detail:** use a `useEffect` that watches
`executions.length` (not `executions`) to trigger scroll-to-bottom. Watching the
full array causes unnecessary scroll interruptions mid-stream.

```typescript
interface OutputPaneProps {
  executions: ExecutionRecord[];
}
```

CSS: `flex: 1; overflow-y: auto; padding: 12px 16px;`

---

### `src/components/ExecutionBlock.tsx`

Renders one execution. Visual structure:

```
┌─────────────────────────────────────────────────────┐
│ [timestamp]  show @lexica//models:bert          [●]  │ ← header
├─────────────────────────────────────────────────────┤
│ > show @lexica//models:bert                          │
│ struct(name="bert", ...)                             │ ← body (output lines)
└─────────────────────────────────────────────────────┘
```

- Header shows: timestamp (formatted `HH:MM:SS`), the command text, and a status
  indicator (spinner SVG while running, checkmark when done, X when error).
- Body is elastic: grows with content up to `max-height: 320px`, then scrolls
  internally (`overflow-y: auto`).
- Each `OutputChunk` is a `<span>` with a class derived from `chunk.kind`
  (`stdout` → default color, `stderr` → red, `meta` → dimmed).
- Output lines are separated by `<br />` or rendered as `<pre>` to preserve
  whitespace.

```typescript
interface ExecutionBlockProps {
  record: ExecutionRecord;
}
```

---

### `src/components/InputBar.tsx`

Auto-growing textarea anchored at the bottom of the screen. Pressing Enter
(without Shift) submits the command and clears the input.

**Sizing:** use a `<textarea>` with `rows={1}`. Grow it dynamically by setting
`height: auto` in a `useEffect` that fires when `value` changes:

```typescript
useEffect(() => {
  if (textareaRef.current) {
    textareaRef.current.style.height = "auto";
    textareaRef.current.style.height = `${textareaRef.current.scrollHeight}px`;
  }
}, [value]);
```

Cap vertical growth at `max-height: 200px` via CSS; beyond that, the textarea
scrolls internally.

**Key handler:**

```typescript
const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (value.trim()) {
      onSubmit(value.trim());
      setValue("");
    }
  }
};
```

**Props:**

```typescript
interface InputBarProps {
  onSubmit: (command: string) => void;
  disabled?: boolean; // true while an execution is in-flight (optional)
}
```

**Future evolution hook:** The `onSubmit` signature takes a plain `string` now.
When the structured input is introduced, `InputBar` becomes a thin shell
delegating to a `StructuredInput` subcomponent. The `onSubmit` signature stays
the same — the structured fields (command, targets, settings) are serialized to
a canonical string before being handed up. This means `ReplPage.handleSubmit`
never needs to change.

**CSS:**

```css
.inputBar {
  display: flex;
  align-items: flex-end;
  padding: 12px 16px;
  border-top: 1px solid #2a2a4a;
  background: #12122a;
  gap: 8px;
}

.textarea {
  flex: 1;
  resize: none;
  overflow-y: auto;
  max-height: 200px;
  min-height: 36px;
  padding: 8px 12px;
  border-radius: 6px;
  border: 1px solid #3a3a5c;
  background: #1e1e3f;
  color: #e0e0e0;
  font-family: inherit;
  font-size: inherit;
  line-height: 1.5;
}
```

---

### `src/components/GearIcon.tsx`

A simple SVG gear icon wrapped in a `<button>` or `<Link>` that navigates to
`/settings`. Positioned absolutely at `top: 12px; right: 16px` relative to the
Layout container (Layout must set `position: relative`).

```typescript
import { Link } from "react-router-dom";

export function GearIcon() {
  return (
    <Link to="/settings" className={styles.gearLink} aria-label="Settings">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
        {/* standard gear path — see implementation note below */}
      </svg>
    </Link>
  );
}
```

Implementation note: use the standard Material Design settings gear path
(`M19.14,12.94c...`) or any public-domain SVG path. No icon library dependency
is introduced.

---

### `src/pages/SettingsPage.tsx`

Placeholder. Displays a heading and a back link. No state.

```tsx
import { Link } from "react-router-dom";

export function SettingsPage() {
  return (
    <div className={styles.page}>
      <h1>Settings</h1>
      <p>Settings will appear here.</p>
      <Link to="/">Back to REPL</Link>
    </div>
  );
}
```

---

## Layout Design

```
┌─────────────────────────────────────────┐  ← 100vh
│ ⚙                              [gear]   │  ← position: absolute top-right
│                                         │
│  OutputPane                             │  ← flex: 1, overflow-y: auto
│  ┌───────────────────────────────────┐  │
│  │ ExecutionBlock (done)             │  │
│  ├───────────────────────────────────┤  │
│  │ ExecutionBlock (running)     [●]  │  │
│  │   > show @lexica//...             │  │  ← max-height: 320px, scrolls
│  └───────────────────────────────────┘  │
│                                         │
├─────────────────────────────────────────┤  ← border-top
│ InputBar                                │  ← shrink-to-content
│ ┌─────────────────────────────────┐     │
│ │ textarea (max-height: 200px)    │     │
│ └─────────────────────────────────┘     │
└─────────────────────────────────────────┘
```

The `Layout` flex column has `overflow: hidden` on the root so that the
`OutputPane` (not the page) scrolls. The `InputBar` never moves — it is always
visible at the bottom regardless of output volume.

---

## Command Execution Flow

```
1. User types "show @lexica//models:bert" in InputBar textarea
2. User presses Enter (not Shift+Enter)
3. InputBar.handleKeyDown fires:
     e.preventDefault()
     onSubmit("show @lexica//models:bert")
     setValue("")
     textarea height resets to auto → 36px
4. ReplPage.handleSubmit("show @lexica//models:bert"):
     id = createExecutionId()
     record = { id, command, submittedAt: new Date().toISOString(),
                status: "running", output: [] }
     setExecutions(prev => [...prev, record])
5. executor.run(record.command, onChunk) called (non-blocking, .then/.catch)
6. Stub executor emits:
     onChunk({ text: "> show @lexica//models:bert", kind: "meta" })
     await 120ms
     onChunk({ text: "show @lexica//models:bert", kind: "stdout" })
   Each onChunk call:
     setExecutions(prev => prev.map(r =>
       r.id === id ? { ...r, output: [...r.output, chunk] } : r
     ))
7. executor.run resolves:
     setExecutions(prev => prev.map(r =>
       r.id === id ? { ...r, status: "done" } : r
     ))
8. OutputPane re-renders; ExecutionBlock shows checkmark
9. OutputPane scrolls to bottom (useEffect on executions.length)
```

Error path: if `executor.run` rejects, status becomes `"error"` and the error
message is pushed as `{ text: error.message, kind: "stderr" }`.

---

## Settings Page

Routing approach: React Router v7 with `BrowserRouter`. Two routes: `"/"` →
`ReplPage`, `"/settings"` → `SettingsPage`.

Hash routing (`HashRouter`) is an acceptable alternative if the dev server does
not support history-API fallback, but `python3 -m http.server` serves static
files at root so `BrowserRouter` works for direct `/` access. Deep links to
`/settings` will 404 on a cold load from the dev server — acceptable for a dev
shell. Switch to `HashRouter` if this becomes a friction point.

SettingsPage scope for this version: heading, explanatory text, back link. No
form fields, no persistence. The page exists so that the gear icon has somewhere
to go and the routing infrastructure is proven.

---

## File Structure

```
mlody/stage/
├── BUILD.bazel           Bazel targets (see below)
├── index.html            HTML shell
├── serve.sh              Dev server launcher
├── tsconfig.json         TypeScript config (copy from smoketest, unchanged)
└── src/
    ├── main.tsx
    ├── App.tsx
    ├── App.module.css
    ├── types.ts
    ├── executor.ts
    ├── components/
    │   ├── Layout.tsx
    │   ├── Layout.module.css
    │   ├── OutputPane.tsx
    │   ├── OutputPane.module.css
    │   ├── ExecutionBlock.tsx
    │   ├── ExecutionBlock.module.css
    │   ├── InputBar.tsx
    │   ├── InputBar.module.css
    │   ├── GearIcon.tsx
    │   └── GearIcon.module.css
    └── pages/
        ├── ReplPage.tsx
        ├── ReplPage.module.css
        ├── SettingsPage.tsx
        └── SettingsPage.module.css
```

CSS Modules are co-located with their component. Global resets live in a
`src/index.css` imported from `main.tsx` (or inlined into `index.html`).

---

## Bazel Build Targets (`BUILD.bazel`)

```python
load("@aspect_rules_esbuild//esbuild:defs.bzl", "esbuild_bundle")
load("@aspect_rules_ts//ts:defs.bzl", "ts_project")
load("@rules_shell//shell:sh_binary.bzl", "sh_binary")

# Dev server: bazel run //mlody/stage:devserver
sh_binary(
    name = "devserver",
    srcs = ["serve.sh"],
    data = [":app"],
)

# TypeScript compilation
ts_project(
    name = "ts",
    srcs = glob(["src/**/*.tsx", "src/**/*.ts"]),
    declaration = True,
    transpiler = "tsc",
    tsconfig = ":tsconfig.json",
    deps = [
        "//:node_modules/@types/react",
        "//:node_modules/@types/react-dom",
        "//:node_modules/react",
        "//:node_modules/react-dom",
        "//:node_modules/react-router-dom",
        "//:node_modules/@types/react-router-dom",
    ],
)

# Bundle: esbuild bundles the compiled JS into a single browser file
esbuild_bundle(
    name = "bundle",
    srcs = [":ts"],
    entry_point = "src/main.js",
    output = "bundle.js",
    tsconfig = ":tsconfig.json",
    format = "esm",
    platform = "browser",
    deps = [
        "//:node_modules/react",
        "//:node_modules/react-dom",
        "//:node_modules/react-router-dom",
    ],
)

# Static assets for the dev server
filegroup(
    name = "app",
    srcs = [
        "index.html",
        ":bundle",
    ],
)
```

**Note on CSS Modules:** `ts_project` with `tsc` transpiler does not natively
process CSS Modules. Two options:

1. **Use plain `className` strings** — simplest, no tooling change. Namespace by
   component name convention (`ExecutionBlock-header`, etc.).
2. **Switch to esbuild for CSS handling** — esbuild supports CSS Modules
   natively when `loader: { '.css': 'local-css' }` is configured via an esbuild
   plugin or `esbuild_bundle` args.

**Recommended for v1:** Use option 1 (plain class name strings). The component
CSS files are imported as side-effect stylesheets and class names are plain
string constants. This matches what `tsc` can handle without extra bundler
configuration. Revisit CSS Modules if the stylesheet footprint grows unwieldy.

---

## Dev Server

**Build:**

```bash
bazel build //mlody/stage:app
```

**Run:**

```bash
bazel run //mlody/stage:devserver
# Serves on http://localhost:8000
PORT=9000 bazel run //mlody/stage:devserver
```

`serve.sh` content (identical pattern to smoketest):

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$0.runfiles/_main/mlody/stage"
cd "$ROOT"
exec python3 -m http.server "${PORT:-8000}"
```

After each source change: `bazel build //mlody/stage:app`, then hard-refresh the
browser. No hot-reload in this setup.

---

## npm Package Changes Required

Before the Bazel build can succeed, add `react-router-dom` to the workspace:

```bash
# From repo root
pnpm add react-router-dom
pnpm add -D @types/react-router-dom
```

Then verify that `pnpm-lock.yaml` is updated and commit it. The Bazel
`node_modules` targets are derived from `pnpm-lock.yaml` via `aspect_rules_js`.

---

## Future-Proofing Notes

### Transition to structured input

The current `InputBar` accepts a single `string` via `onSubmit`. When the
structured input is introduced:

1. Add a `StructuredInput` component alongside `InputBar` with the same
   `onSubmit: (command: string) => void` prop.
2. `StructuredInput` renders three subfields:
   - `CommandField` — constrained to known verbs (`show`, `system`, …)
   - `TargetsField` — text input with LSP-backed autocompletion (see below)
   - Settings are removed to the settings page; no inline settings field.
3. `ReplPage` swaps `<InputBar>` for `<StructuredInput>` with no other changes.
4. The `ExecutionRecord` type is unchanged because the command is always
   serialized to a string before storage.

### LSP autocompletion integration

The mlody LSP server (`mlody/lsp/`) is a pygls server that communicates over
stdio. To expose it to the browser:

1. Add a WebSocket bridge (a thin Python process) that speaks LSP on one side
   and WebSocket on the other. This bridge runs alongside the dev server.
2. `TargetsField` sends `textDocument/completion` requests over the WebSocket
   and renders the response as a dropdown.
3. Target syntax `@ROOT//package:target.field` is the LSP's native completion
   domain — no new server-side features are needed for basic completion.

The `executor.ts` abstraction already accommodates WebSocket: replace
`stubExecutor` with a `WebSocketExecutor` that opens a connection, sends the
command as a JSON message, and streams response chunks as they arrive.

### Command dispatch

When real execution is wired up:

- `executor.run(command, onChunk)` connects to a backend endpoint (HTTP SSE or
  WebSocket).
- The backend parses the command verb (`show`, `system`, …) and dispatches to
  the appropriate mlody subsystem.
- `ExecutionRecord.output` already accommodates mixed stdout/stderr/meta chunks.
- No UI changes are required.

### Settings persistence

When settings are real:

- Store in `localStorage` under a namespaced key (`mlody.stage.settings`).
- Expose a `useSettings()` hook from a `src/hooks/useSettings.ts` module.
- `SettingsPage` renders a form that calls `useSettings().update(...)`.
- `ReplPage` reads settings (e.g. LSP endpoint URL) via `useSettings()`.

---

## Implementation Plan

### Phase 1 — Scaffold (no functionality)

1. Create `mlody/stage/` directory structure (all files, empty components).
2. Write `index.html`, `tsconfig.json`, `serve.sh`, `BUILD.bazel`.
3. Add `react-router-dom` to root `package.json`; run `pnpm install`.
4. Verify `bazel build //mlody/stage:app` passes.
5. Verify `bazel run //mlody/stage:devserver` serves the page.

### Phase 2 — Layout and routing

6. Implement `Layout`, `App` with routes, `ReplPage` skeleton, `SettingsPage`
   placeholder, `GearIcon`.
7. Verify navigation between `/` and `/settings` works.

### Phase 3 — Input component

8. Implement `InputBar` with auto-grow and Enter-to-submit.
9. Wire `onSubmit` to a `console.log` stub in `ReplPage`.
10. Verify textarea grows, caps at 200px, clears on submit.

### Phase 4 — Execution and output

11. Implement `types.ts`, `executor.ts` (stub).
12. Implement `ExecutionBlock` (header + elastic body).
13. Implement `OutputPane` with scroll-to-bottom.
14. Wire everything in `ReplPage.handleSubmit`.
15. Verify the full echo flow: type → Enter → block appears → output streams in.

### Phase 5 — Polish

16. Apply CSS: dark theme, monospace font, status indicators.
17. Add loading spinner SVG to `ExecutionBlock` for `status: "running"`.
18. Final review against this spec.

---

## Non-Functional Requirements

| Requirement        | Target                                                                           |
| ------------------ | -------------------------------------------------------------------------------- |
| Bundle size        | < 200 KB gzipped (React + Router + app code)                                     |
| Input latency      | Keystrokes render within one animation frame (no debounce on input)              |
| Scroll performance | `OutputPane` handles 100+ execution records without jank                         |
| Accessibility      | Textarea has `aria-label="Command input"`; gear link has `aria-label="Settings"` |
| Browser support    | Chrome/Firefox/Safari latest two versions                                        |

---

## Risks and Mitigations

| Risk                                                                  | Likelihood | Mitigation                                                                                                 |
| --------------------------------------------------------------------- | ---------- | ---------------------------------------------------------------------------------------------------------- |
| `esbuild_bundle` does not bundle CSS imports from `ts_project` output | Medium     | Use plain string class names (option 1 above); defer CSS Modules                                           |
| `react-router-dom` not in workspace `node_modules` Bazel map          | Medium     | Add to `package.json`, run `pnpm install`, verify Bazel sees it                                            |
| `BrowserRouter` causes 404 on `/settings` cold load                   | Low        | Acceptable for dev shell; switch to `HashRouter` if needed                                                 |
| `NodeNext` module resolution breaks relative imports in esbuild       | Low        | Use `.js` extensions on all relative imports (TypeScript NodeNext convention); smoketest already does this |

---

## Future Considerations

- **Test harness:** Add a `vitest` or `@bazel/jest` target under
  `//mlody/stage:test` once the component logic is non-trivial.
- **Hot reload:** Replace `python3 -m http.server` with `vite --host` or
  `esbuild --serve` for instant feedback during development.
- **LSP WebSocket bridge:** A small Python `asyncio` server wrapping
  `mlody/lsp/server.py` over WebSocket instead of stdio.
- **Command history:** `InputBar` tracks previous commands; Up/Down arrows cycle
  through them (stored in `useRef`, not persisted).
- **Multi-pane layout:** Split `OutputPane` into a results pane and a detail
  pane for structured output (e.g., struct field inspector).
