# codemap

**Interactive architecture mapper for Spring Boot applications.**

Scans Kotlin and Java source, infers components from Spring annotations, and produces:

- An **interactive HTML map** — click any endpoint to trace its full call chain through services, repositories, and external systems
- **`architecture.md`** — Mermaid diagrams and endpoint tables, ready to commit or feed to an AI

No annotations required. No configuration. Point it at a repo root.

---

## Two reasons to use it

### 1. Living documentation

Keep an always-current architecture map published to GitHub Pages. Regenerates automatically on every push to main — no manual upkeep.

```
https://<org>.github.io/<repo>/
```

### 2. AI-assisted development

When building or refactoring with an AI coding assistant, the assistant needs to understand how the system is structured — which services exist, what they depend on, which endpoints call what. Without that context it makes changes in the dark.

Run codemap before a session and hand the output to the AI:

```bash
codemap . --no-html   # writes architecture.md
```

Then in your AI session:
> "Here is the current architecture: [architecture.md]. I want to add an endpoint that…"

The AI now knows the full component graph before writing a single line of code.

---

## Install

```bash
pipx install git+https://github.com/esschul/codemap.git
```

Requires Python 3.9+. Java 11+ on `PATH` is optional — enables more accurate scanning of Java files via a bundled JavaParser AST scanner.

## Usage

Run from the root of any Spring Boot repository:

```bash
codemap . --serve
```

Opens `http://localhost:8742/` in your browser. Source root (`src/main`) is detected automatically.

```
codemap [root] [--title "Name"] [--serve] [--port 8742]
               [--name SLUG]        # multi-project: localhost:8742/slug/
               [--docs DIR]         # one markdown per endpoint + index.md
               [--watch]            # re-scan every --interval seconds
               [--interval 120]
               [--md architecture.md]
               [--no-md] [--no-html]
               [--list]             # print component table to stdout
```

---

## Publish to GitHub Pages

Turn the map into a living document that updates itself every time code lands on main.

**Step 1 — Add the workflow to your repo**

```bash
mkdir -p .github/workflows
curl -fsSL https://raw.githubusercontent.com/esschul/codemap/master/contrib/codemap-pages.yml \
  -o .github/workflows/codemap.yml
git add .github/workflows/codemap.yml
git commit -m "Add architecture map"
git push
```

**Step 2 — Enable GitHub Pages**

Go to **Settings → Pages → Source** and set it to the `gh-pages` branch.

**Step 3 — Done**

GitHub runs the workflow automatically. The map is live at `https://<org>.github.io/<repo>/` within a couple of minutes and stays current from then on.

The workflow triggers on:
- Every push to `main` or `master`
- Every Monday at 06:00 UTC (catches drift even in quiet periods)
- Manual dispatch from the Actions tab

GitHub Actions is GitHub's built-in automation — it's free for public repos and requires no external services. Each codemap run takes about 30 seconds.

---

## What it detects

| Colour | Kind | Detected by |
|---|---|---|
| Blue | Controller | `@RestController`, `@Controller`, name suffix |
| Green | Service | `@Service`, name suffix |
| Purple | Repository | `@Repository`, name suffix |
| Amber | Client | `@FeignClient`, name suffix (`Client`, `Gateway`, `Adapter`) |
| Teal | Consumer | `@KafkaListener`, name suffix |
| Cyan | Mapper / Builder | name suffix |
| Dark | External system | injected types (`JpaRepository`, `KafkaTemplate`, `S3Client`, …) |

External systems are inferred from injected field types — no annotations needed.

---

## Development

```bash
git clone https://github.com/esschul/codemap.git
cd codemap
pip install -e .
python3 -m pytest -q
```

Rebuild the Java AST scanner after changing `ast_scanner/src/main/java/dev/codemap/Scanner.java`:

```bash
mvn -q -f ast_scanner/pom.xml package
cp ast_scanner/target/ast-scanner.jar codemap/ast-scanner.jar
```
