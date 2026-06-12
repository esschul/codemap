# codemap

Interactive Spring Boot architecture mapper.

`springmap.py` scans Kotlin and Java Spring Boot source, infers application components, and generates:

- `architecture.md` with Mermaid diagrams and endpoint tables
- an interactive HTML map for endpoint call chains and component dependencies

It works without project annotations, and can optionally enrich output from `@AppMap` annotations when present.

## Requirements

- Python 3.9+
- Java 11+ on `PATH`
- Maven, only when rebuilding the Java AST scanner

## Quick Start

From a Spring Boot repository:

```bash
python3 /path/to/codemap/springmap.py src/main \
  --title "My Application Architecture" \
  --serve
```

With `--serve`, codemap writes the HTML to a fixed internal serve directory, starts a local server, and opens:

```text
http://localhost:8742/
```

It also writes `architecture.md` in the current working directory.

## Install For Personal Use

The mapper can be bootstrapped into `~/.codemap`:

```bash
mkdir -p ~/.codemap/ast_scanner/target
curl -fsSL https://raw.githubusercontent.com/esschul/codemap/master/springmap.py \
  -o ~/.codemap/springmap.py
curl -fsSL https://github.com/esschul/codemap/raw/master/ast_scanner/target/ast-scanner.jar \
  -o ~/.codemap/ast_scanner/target/ast-scanner.jar
```

Then run:

```bash
python3 ~/.codemap/springmap.py src/main \
  --title "My Application Architecture" \
  --serve
```

## CLI Usage

```bash
python3 springmap.py [root] [options]
```

Common options:

- `--title "Name"`: set the map title
- `--serve`: serve the HTML map locally and open the browser
- `--port 8742`: choose the serve port
- `--md architecture.md`: choose the Markdown output path
- `--html appmap.html`: choose the HTML output path when not using the fixed serve flow
- `--no-html`: only generate Markdown
- `--no-md`: only generate HTML
- `--list`: print detected components instead of writing files

## What It Detects

codemap infers:

- controllers and HTTP endpoints
- services and injected dependencies
- repositories and database access boundaries
- clients and external systems
- schedulers, consumers, mappers, validators, gateways, and generic components

For Java files, `springmap.py` uses the bundled JavaParser-based scanner at:

```text
ast_scanner/target/ast-scanner.jar
```

If the JAR is missing or Java is unavailable, the tool falls back to regex-based scanning.

## Rebuilding The AST Scanner

After changing `ast_scanner/src/main/java/dev/codemap/Scanner.java`, rebuild the bundled JAR:

```bash
mvn -q -f ast_scanner/pom.xml package
```

The resulting runtime JAR is:

```text
ast_scanner/target/ast-scanner.jar
```

## Development

Run regression tests:

```bash
python3 -m pytest -q -p no:cacheprovider
```

Run a sample scan without writing HTML to the fixed serve directory:

```bash
python3 springmap.py fixture-spring/src/main \
  --no-html \
  --md /tmp/codemap-architecture.md \
  --title "Fixture Architecture"
```

## Notes

- `--serve` intentionally reuses `http://localhost:8742/` so the browser URL is stable across projects.
- Generated local outputs such as `architecture.md`, `appmap.html`, and build caches are ignored by `.gitignore`.
