# codemap

Interactive Spring Boot architecture mapper.

Scans Kotlin and Java Spring Boot source, infers application components, and generates:

- An interactive HTML map for endpoint call chains and component dependencies
- `architecture.md` with Mermaid diagrams and endpoint tables

Works without project annotations. Java files are optionally enriched by a bundled JavaParser AST scanner.

## Requirements

- Python 3.9+
- Java 11+ on `PATH` (optional — enables more accurate Java scanning)

## Install

```bash
pipx install git+https://github.com/esschul/codemap.git
```

Or with plain pip:

```bash
pip install git+https://github.com/esschul/codemap.git
```

## Usage

Run from the root of any Spring Boot repository — source root is detected automatically:

```bash
codemap . --serve
```

Opens `http://localhost:8742/` in your browser. Pass `--title` to label the map:

```bash
codemap . --title "My App" --serve
```

## Options

```
codemap [root] [--title "Name"] [--serve] [--port 8742]
               [--name SLUG]        # multi-project server (localhost:8742/slug/)
               [--docs DIR]         # one markdown per endpoint + index.md
               [--watch]            # re-scan every --interval seconds
               [--interval 120]     # seconds between scans (default: 120)
               [--md architecture.md]
               [--no-md]
               [--no-html]
               [--list]             # print component table to stdout
```

## What It Detects

- Controllers and HTTP endpoints
- Services and injected dependencies
- Repositories and database access boundaries
- Clients and external systems (Feign, RestTemplate, WebClient, Kafka, S3, …)
- Schedulers, consumers, mappers, validators, gateways, and generic components

## Development

```bash
git clone https://github.com/esschul/codemap.git
cd codemap
pip install -e .
```

Run tests:

```bash
python3 -m pytest -q
```

Rebuild the Java AST scanner after changing `ast_scanner/src/main/java/dev/codemap/Scanner.java`:

```bash
mvn -q -f ast_scanner/pom.xml package
cp ast_scanner/target/ast-scanner.jar codemap/ast-scanner.jar
```
