# Discussion: LLM-supercharged documentation

This document exists to think through what a local LLM integration could add to codemap,
and to invite input from other contributors (human or AI) before any implementation starts.

Bring this into a Codex or Claude session with:
> "Read DISCUSSION.md and share your thoughts on the proposed LLM integration for codemap."

---

## What codemap gives you today

- Component graph: controllers, services, repos, clients, their dependencies
- HTTP endpoints and call chains
- External systems (kafka, database, s3, …)
- Non-HTTP entrypoints (scheduled jobs, kafka listeners, event listeners)
- Statistics: LOC, fan-in/fan-out, orphans, domain breakdown

**What it does not give you:** any understanding of *why* things exist, *what* a component
actually does, or whether the architecture looks healthy.

---

## What a local LLM could add

### 1. Endpoint descriptions

Given: HTTP method + path + handler name + full call chain + external systems reached.

Generate: one or two sentences explaining what the endpoint does in plain English.

```
GET /orders/{id}
→ OrderController.getOrder()
→ OrderService → OrderRepository (database), InventoryClient (inventory-service)

LLM output:
"Fetches a single order by ID. Checks current fulfilment status via InventoryClient
before returning. Returns 404 if the order does not exist."
```

Displayed inline in the chain view and in the per-endpoint markdown docs.

### 2. Domain summaries

Given: all components in a domain, their kinds, dependencies, external systems.

Generate: a short paragraph describing what the domain owns and how it interacts with the rest.

```
Payment domain: PaymentController, PaymentService, PaymentRepository,
StripeClient → externals: stripe-api, database

LLM output:
"The payment domain handles charge initiation and refunds. PaymentService
orchestrates the flow — it calls StripeClient for payment processing and
persists results via PaymentRepository. It has no direct dependency on other
domains, making it a good candidate for extraction."
```

### 3. Architectural observations

Scan the component graph for patterns worth surfacing:

- High fan-in components (many callers): are they generic utilities or hidden bottlenecks?
- Controllers with many direct service dependencies: possible god controller
- Services reaching external systems directly without a client abstraction
- Circular dependency chains
- Isolated components with no callers and no dependencies (orphans)
- Domain boundaries violated (e.g. OrderController depending on PaymentRepository)

These would appear as a dedicated "Observations" section in the Stats overlay.

### 4. Change narration (watch mode)

When `--watch` triggers a rescan and finds differences, narrate what changed:

```
Before: 42 components
After:  45 components

LLM output:
"Added CheckoutService (service) with dependencies on OrderService and
PaymentService. A new endpoint POST /checkout now routes through it.
CheckoutService reaches the kafka external system — the first component
in the order domain to do so."
```

Shown in the reload banner instead of just "3 → 45 components".

### 5. Chat interface in the HTML map

A small input box in the explorer where you can ask questions about the loaded architecture:

- "Which services can reach kafka?"
- "What calls PaymentRepository?"
- "What is the call chain for POST /orders?"
- "Which components have no callers?"

The component graph and sidebar data are already serialised into the page as JSON,
so the LLM has full context without additional scanning.

### 6. architecture.md narrative

Instead of (or alongside) the Mermaid diagrams, generate a readable architectural
overview in prose — useful as a starting point for a system design doc or for
feeding into a larger AI-assisted documentation workflow.

---

## Detection and fallback

Check for a local LLM in this order:

1. **Ollama** — `GET http://localhost:11434/api/tags` — if reachable, use the best
   available model (prefer `llama3`, `mistral`, `phi3` in that order)
2. **LM Studio** — `GET http://localhost:1234/v1/models`
3. **`ANTHROPIC_API_KEY`** env var — cloud fallback via Claude API
4. **Nothing available** — skip all LLM steps, behave exactly as today

Additions should be purely additive. If no LLM is found, the tool produces
identical output to the current version.

---

## Open questions for discussion

1. **Granularity**: generate descriptions per endpoint, per component, or per domain?
   Generating everything is slow; generating on demand (click to generate) is more
   practical but requires a running LLM at browse time.

2. **Caching**: LLM output should be cached to disk (keyed by file hash + component name)
   so re-scans don't re-generate unchanged components. Where should the cache live?

3. **Prompt design**: what context does the LLM need per request?
   - Just the call chain? Or the full component graph?
   - Should it see the actual source file?
   - How do we keep prompts small enough to be fast on a local model?

4. **Quality bar**: local models (7B–13B) will produce uneven output. Should there be
   a confidence threshold, or show everything and let the user judge?

5. **Chat interface**: this requires the LLM to be reachable at browse time (not just
   scan time). Is that a reasonable assumption for local-first use?

6. **Privacy**: source code content sent to an LLM (even local) may be sensitive.
   Should the tool only send structural data (names, kinds, dependencies) and never
   source text?

---

## Proposed implementation order

If we proceed:

1. `codemap/llm.py` — detection, prompt construction, response parsing
2. Endpoint descriptions first (most immediately useful, smallest context window)
3. Architectural observations second (pure graph analysis, no source needed)
4. Domain summaries third
5. Watch-mode change narration
6. Chat interface last (most complex, requires streaming)

---

## Codex response (recorded)

> The LLM idea is useful, but only if it is treated as an annotation layer over trusted
> structural data, not as part of the scanner's core truth.

**Agreed decisions:**

- Endpoint descriptions first. Structural data only (method/path, handler, call chain,
  component kinds, external systems). No source code in v1.
- Cache key: `scannerVersion + promptVersion + modelId + endpointStructuralJsonHash`
- Cache location: `.codemap/cache/llm/` inside the scanned repo (transparent, easy to delete)
- **Hybrid generation**: cached summaries included if present; missing summaries NOT
  generated by default. Add `--llm-generate endpoint|domain|all` for explicit batch.
  Click-to-generate in UI for missing summaries if a local LLM is reachable.
- Architectural observations: deterministic rules detect the finding (fan-in, orphans,
  cycles, cross-domain deps). LLM only narrates — it does not discover.
- Provenance label: "Generated from structural graph only." No confidence scoring.
- `--no-llm` flag to disable even if a local model is detected.

**On the Kotlin scanner:**

> I would not lock into kotlinx-ast yet. Compare Kotlin PSI/compiler embeddable, KSP,
> tree-sitter Kotlin, and kotlinx-ast before choosing.

This is the right call. Kotlin scanner is a parallel track and does not interact with
LLM work. Better structural data makes later LLM summaries better.

---

## Codex response on Kotlin scanner (second discussion)

> PSI/compiler-embeddable is technically the most Kotlin-native answer, but the friction is a real signal. For codemap, I would not keep pushing it unless you specifically want semantic Kotlin analysis later.

**Agreed decisions:**

- Stop the PSI implementation. The use case (find classes, read annotations, read constructor/property types, extract mapping paths, detect field calls) is concrete syntax tree work — PSI is oversized for it.
- **Do a tree-sitter spike.** tree-sitter is built exactly for lightweight source parsing and structural code queries. No Kotlin compiler classpath. Fits the "read paths on stdin, emit JSON" architecture. Small bundled JAR.
- Avoid `kotlinc -script` as the primary path — moves the problem to user environment reliability. The bundled JAR model (like the Java scanner) is the right property to preserve.
- Require the tree-sitter scanner to beat regex on 4–6 Kotlin fixture cases. If tree-sitter integration is also messy, improve the regex scanner and defer AST.
- Do not adopt compiler PSI unless semantic analysis becomes a real requirement.

**Decision rule:**
- If tree-sitter can produce the Java scanner JSON shape with a bundled JAR and modest code → use it.
- If it requires native packaging pain or fragile grammar work → keep regex for now.

**Real value of an AST scanner over regex (beyond constructor fields):**
- Annotations with named args and arrays
- Multiple annotations between mapping and function
- Nested/private helper calls and expression-bodied functions
- `this.service.foo()` and chained calls
- Functional route/handler patterns
- Reducing false positives from regex lookback windows

---

## Notes from prior conversation

- Kotlin AST scanner (tree-sitter based, same JSON shape as the Java scanner) is a
  separate track and should be implemented independently of the LLM work.
- The LLM integration should never block or slow down the scan if no LLM is available.
- `--no-llm` flag to explicitly disable even if a local model is detected.
