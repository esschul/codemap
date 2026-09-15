---
name: codemap
description: >
  Architecture mapping tool for Kotlin/Java Spring Boot applications. Trigger
  whenever the user says "map the application", "map this repo", "create a
  map", "show me the dependencies", "generate a dependency graph", "what
  services does this endpoint use", "trace the call chain", "show me the
  controllers and services", "document this codebase", or anything implying
  they want to understand how a Spring Boot application is structured —
  including when they switch to a new repo and want to orient themselves.
  Also trigger on "/codemap". Generates an interactive HTML explorer where you
  can click any endpoint and see its full call chain through services,
  repositories, and external systems.
---

# codemap skill

Generates an interactive architecture map for Spring Boot (Kotlin/Java)
applications in one command. No annotations required — everything is inferred
from Spring annotations and constructor injection.

This skill wraps the `codemap` CLI (https://github.com/esschul/codemap).
Install it once, on any machine, with:

```bash
pipx install git+https://github.com/esschul/codemap.git
```

Java 11+ on `PATH` is optional but recommended — it enables a more accurate
JavaParser-based AST scan of `.java` files.

If `codemap --help` fails, tell the user to install it with the command above
before continuing.

---

## Steps — always do all of these in order

### 1. Find the project name

Check in order:
- `settings.gradle` or `settings.gradle.kts` → `rootProject.name = "foo"`
- `build.gradle.kts` → `project.name` or the directory name
- Fall back to the directory name of the repo root

Use this as `--title`.

### 2. Run the scanner

Run from the **repo root** — the Spring source root (`src/main`) is detected
automatically:

```bash
codemap . --title "<Project Name> Architecture" --serve
```

- **Do NOT pass `src/main` manually** — auto-detection handles it
- `--serve` opens `http://localhost:8742/` in the browser and kills any
  previous server on that port first
- If the user wants a live-updating map while they work, use
  `codemap live .` instead — shorthand for `--serve --watch --debug-diff`

### 3. Report what was found

After the scan, print a brief summary:

```
Found X controllers, Y services, Z repos, W clients across N domains.
External systems: foo, bar, baz.
http://localhost:8742/ is open — click any endpoint to trace its call chain.
```

### 4. Offer to dig in

After the map is open, offer:
- "Want me to generate endpoint documentation? (`codemap . --docs docs/endpoints --no-html --no-md`)"
- "Want me to watch the repo and keep the map up to date? (`codemap . --serve --watch`)"
- "Any component that looks unexpected?"

---

## Answering "what does endpoint X call?" without opening a browser

For a quick, text-only answer (e.g. inside a chat response, or when there's no
browser available), use the `context` subcommand instead of `--serve`:

```bash
codemap context <path-or-handler-pattern> [--depth 3]
# or, if not run from the repo root:
codemap context <root> <path-or-handler-pattern>
```

This prints the full call chain (controller → service → repository → external
system) for every endpoint matching the pattern, as plain text — no server,
no browser. Use this when the user asks a narrow question about one endpoint
rather than wanting the whole map.

For machine-readable output (e.g. to hand the full component graph to another
tool), use `codemap . --json` (or `--json-out FILE`).

---

## What the explorer shows

- **Left sidebar**: HTTP / Jobs / Events / Search tabs
  - HTTP tab: controllers and endpoints — click to trace call chain
  - Jobs tab: `@Scheduled` methods
  - Events tab: `@EventListener` / `@KafkaListener` methods
  - Search tab: search across all components, endpoints, packages, external systems
- **Chain view**: left-to-right call chain SVG when an endpoint is selected
- **Right inspector**: click any node in the chain to see classification,
  dependencies, callers, and a clickable `file://` link to the source file.
  Click a tag to navigate to that component.
- **Toolbar buttons**:
  - **Graph** — full component graph overlay with a Copy image button
  - **Externals** — all external systems and which endpoints can reach them
  - **Stats** — codebase statistics: LOC, fan-in/fan-out, orphans, domain breakdown
  - **Heatmap toggle** — colour nodes by git activity (recency / churn /
    authors) instead of by role; nodes with no git data are greyed out
- **Footer**: scan metadata (git branch, timestamp, scanner version, AST enrichment count)
- **Watch banner**: with `--watch`, a banner appears when a new scan is ready

If the user has a local [Ollama](https://ollama.com) running, the inspector
can also generate an on-demand Mermaid sequence diagram for the selected
component or endpoint flow — this is optional and only activates if Ollama is
detected.

---

## Additional flags

```
codemap [root] [--title "Name"] [--serve] [--port 8742]
               [--docs DIR]           # one markdown per endpoint + index.md
               [--watch]              # re-scan on interval
               [--interval 120]       # seconds between scans (default: 120)
               [--md architecture.md] # markdown output path
               [--no-md]              # skip markdown
               [--no-html]            # skip HTML (useful with --docs only)
               [--list]               # print component table to stdout
               [--json] [--json-out FILE]  # full component/endpoint graph as JSON

codemap live [root]                   # shorthand for --serve --watch --debug-diff
codemap context [root] <pattern>      # print call chain for matching endpoints
```

### Documentation mode

To generate per-endpoint markdown docs (good for committing to the repo):

```bash
codemap . --title "<Project Name>" --docs docs/endpoints --no-html --no-md
```

This writes `docs/endpoints/index.md` (master index with links) and one
`.md` per endpoint (Mermaid call chain, components table, external systems).

### Watch mode

To keep the map up to date while working:

```bash
codemap . --title "<Project Name>" --serve --watch --interval 120
```

The browser shows a blue banner when a new scan is ready, with a Reload button.

---

## Colour guide

| Colour | Kind |
|---|---|
| Blue | Controller |
| Green | Service |
| Purple | Repository |
| Amber | Client (Feign / HTTP / Invoker) |
| Red | Gateway |
| Teal | Consumer (Kafka) |
| Cyan | Mapper / Builder |
| Emerald | Cache |
| Stone | Config / Properties |
| Slate | Component |
| Dark | External system |
