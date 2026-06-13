#!/usr/bin/env python3
"""
springmap.py — Interactive Spring Boot architecture mapper.

No annotations required. Scans Kotlin source and infers:
  • Controllers and their HTTP endpoints
  • Services and their injected dependencies
  • Repositories and backing stores
  • External clients (Feign, RestTemplate, WebClient, Kafka, S3, …)

Generates:
  appmap.html     — interactive dependency explorer (open in browser)
  architecture.md — Mermaid overview diagrams

Enriched automatically by @AppMap annotations when present.

Usage:
    python3 springmap.py [root] [--html appmap.html] [--md architecture.md]
"""

import re, sys, json, argparse, textwrap, shutil, subprocess
from pathlib import Path
from dataclasses import dataclass, field as dc_field
from typing import Optional
from collections import defaultdict

# ── Patterns ─────────────────────────────────────────────────────────────────

PACKAGE_RE = re.compile(r'^\s*package\s+([\w.]+)', re.MULTILINE)

# Kotlin class declaration (skip data/enum/sealed)
KT_CLASS_RE = re.compile(
    r'(?:data\s+|sealed\s+|open\s+|abstract\s+|inner\s+)*'
    r'(?P<kw>class|object|interface)\s+(?P<name>\w+)'
)

# Java class declaration
JAVA_CLASS_RE = re.compile(
    r'(?:public|protected|private)?\s*(?:abstract|final|static)?\s*'
    r'(?P<kw>class|interface|enum)\s+(?P<name>\w+)'
)

# Java: skip pure enum / pure interface / @Entity domain objects
JAVA_SKIP_RE = re.compile(r'\benum\s+\w+|\binterface\s+\w+')

# Java package-level domain path fragments to skip (lots of DTOs/models)
JAVA_SKIP_PATHS = ('domain/', 'model/', 'dto/', 'ws/', 'util/', 'filter/',
                   'swagger/', 'exception/')

# Packages that are always noise regardless of language
SKIP_FRAGMENTS = ('Test.kt', 'Spec.kt', 'Test.java', '/test/', '/generated/',
                  '/build/', '/.gradle/', 'ObjectFactory')

# Spring annotations → architectural role
ROLE_ANNOTATIONS: dict[str, str] = {
    'RestController': 'CONTROLLER',
    'Controller':     'CONTROLLER',
    'Service':        'SERVICE',
    'Repository':     'REPOSITORY',
    'FeignClient':    'CLIENT',
    'Component':      'COMPONENT',
    'Configuration':  'CONFIG',
    'KafkaListener':  'CONSUMER',
    'Scheduled':      'SCHEDULER',
    'EventListener':  'LISTENER',
    'Endpoint':       'GATEWAY',   # Spring Boot Actuator endpoints
}

# HTTP mapping annotations → HTTP method
HTTP_ANNOTATIONS: dict[str, str] = {
    'GetMapping':    'GET',
    'PostMapping':   'POST',
    'PutMapping':    'PUT',
    'DeleteMapping': 'DELETE',
    'PatchMapping':  'PATCH',
    'RequestMapping': 'ANY',
}

# Injected type patterns → inferred external system label
EXTERNAL_HINTS: list[tuple[str, str]] = [
    (r'JpaRepository|CrudRepository|MongoRepository|R2dbcRepository|JdbcTemplate|NamedParameterJdbcTemplate', 'database'),
    (r'KafkaTemplate|KafkaSender|KafkaProducer',      'kafka'),
    (r'RestTemplate|WebClient|FeignClient|HttpClient|RestClient', 'http'),
    (r'AmazonS3|S3Client|S3AsyncClient',              's3'),
    (r'RedisTemplate|ReactiveRedisTemplate',           'redis'),
    (r'ElasticsearchOperations|ElasticsearchClient',   'elasticsearch'),
    (r'MongoTemplate|ReactiveMongoTemplate',           'mongodb'),
    (r'SqsClient|SqsAsyncClient|SqsTemplate',         'sqs'),
    (r'DynamoDbClient|DynamoDbEnhancedClient',         'dynamodb'),
    (r'JavaMailSender|MailSender',                     'email'),
    (r'FirebaseMessaging|FirebaseApp',                 'firebase'),
    (r'OpenSearchClient|RestHighLevelClient',          'opensearch'),
]

# Regex to detect RestClient/RestTemplate/WebClient usage in class body
REST_CLIENT_BODY_RE = re.compile(
    r'\b(RestClient|RestTemplate|WebClient|restClient|restTemplate|webClient)\b'
)

SKIP_PATH_FRAGMENTS = ('Test.kt', 'Spec.kt', 'test/', 'generated/', '/build/', '/.gradle/')


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Endpoint:
    http_method: str          # GET / POST / PUT / DELETE / PATCH / ANY
    path: str                 # combined base + method path
    handler: str              # Kotlin method name
    calls: list[str] = dc_field(default_factory=list)   # service class names used in body


@dataclass
class NonHttpEntrypoint:
    kind: str        # SCHEDULED | KAFKA | EVENT
    method: str      # method name
    detail: str      # topic / cron / event type if known


@dataclass
class Component:
    name: str
    kind: str                 # CONTROLLER | SERVICE | REPOSITORY | CLIENT | …
    package: str
    file: str
    base_path: str = ''       # @RequestMapping on the class
    endpoints: list[Endpoint] = dc_field(default_factory=list)
    non_http_entrypoints: list[NonHttpEntrypoint] = dc_field(default_factory=list)
    dependencies: list[str] = dc_field(default_factory=list)     # other component names
    field_map: dict[str, str] = dc_field(default_factory=dict)   # fieldName → TypeName
    external_systems: list[str] = dc_field(default_factory=list)
    spring_annotations: list[str] = dc_field(default_factory=list)
    classification_reason: str = ''   # human-readable: which annotation/suffix triggered kind
    domain: str = ''
    capability: str = ''
    loc: int = 0


# ── Low-level parsers ─────────────────────────────────────────────────────────

def _matching_paren(text: str, start: int) -> int:
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                return i
    return -1


def _matching_brace(text: str, start: int) -> int:
    depth, in_str, i = 0, False, start
    while i < len(text):
        c = text[i]
        if c == '"' and (i == 0 or text[i - 1] != '\\'):
            in_str = not in_str
        elif not in_str:
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def _annotation_path(ann_text: str) -> str:
    """Extract the first string literal from an annotation body."""
    m = re.search(r'"([^"]*)"', ann_text)
    return m.group(1) if m else ''


def _html_escape(value: object) -> str:
    """Escape text before embedding it in HTML/JS template literals."""
    return (
        str(value)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
        .replace("'", '&#39;')
    )


def _json_for_script(value: object) -> str:
    """Serialize JSON safely for inline <script> blocks."""
    return (
        json.dumps(value)
        .replace('&', '\\u0026')
        .replace('<', '\\u003c')
        .replace('>', '\\u003e')
        .replace('\u2028', '\\u2028')
        .replace('\u2029', '\\u2029')
    )


def _kotlin_constructor_fields(text: str, class_pos: int) -> dict[str, str]:
    """Return {fieldName: TypeName} from Kotlin primary constructor."""
    win = text[class_pos:]
    p = win.find('(')
    if p == -1:
        return {}
    end = _matching_paren(win, p)
    if end == -1:
        return {}
    body = win[p + 1:end]
    result: dict[str, str] = {}
    # Match val/var fields with optional fully qualified types (e.g. no.bring.Foo or Foo)
    for m in re.finditer(
        r'(?:val|var)\s+(\w+)\s*:\s*((?:[\w]+\.)*([A-Z]\w+))(?:<[^>]*>)?', body
    ):
        result[m.group(1)] = m.group(3)  # group(3) = simple class name
    # Also catch non-val/var constructor params (rare but valid Kotlin)
    for m in re.finditer(r'\b(\w+)\s*:\s*((?:[\w]+\.)*([A-Z]\w+))(?:<[^>]*>)?', body):
        if m.group(1) not in result:
            result[m.group(1)] = m.group(3)
    return result


# Skip common Java non-component types injected via @Value / primitives
_JAVA_SKIP_TYPES = frozenset({
    'String', 'Integer', 'Long', 'Boolean', 'Double', 'Float', 'int', 'long',
    'boolean', 'List', 'Map', 'Set', 'Optional', 'Class', 'Duration',
    'HttpServletRequest', 'HttpServletResponse', 'ObjectMapper',
})


def _java_injected_fields(text: str, class_name: str, class_pos: int) -> dict[str, str]:
    """
    Return {fieldName: TypeName} from Java.
    Handles @Autowired fields, constructor injection (including assignment mapping),
    and Lombok @RequiredArgsConstructor (private final fields).
    """
    result: dict[str, str] = {}
    body = text[class_pos:class_pos + 8000]

    # @Autowired or @Inject field: `private TypeName fieldName;`
    field_re = re.compile(
        r'@(?:Autowired|Inject)[^;@]{0,200}?'
        r'(?:private|protected|public)\s+(?:final\s+)?([A-Z]\w+)(?:<[^>]*>)?\s+(\w+)\s*[;=]',
        re.DOTALL
    )
    for m in field_re.finditer(body):
        t, n = m.group(1), m.group(2)
        if t not in _JAVA_SKIP_TYPES:
            result[n] = t

    # Lombok @RequiredArgsConstructor: inject all `private final TypeName field;`
    preamble = text[max(0, class_pos - 500):class_pos]
    if 'RequiredArgsConstructor' in preamble or 'AllArgsConstructor' in preamble:
        lombok_re = re.compile(
            r'private\s+final\s+([A-Z]\w+)(?:<[^>]*>)?\s+(\w+)\s*;'
        )
        for m in lombok_re.finditer(body[:4000]):
            t, n = m.group(1), m.group(2)
            if t not in _JAVA_SKIP_TYPES and n not in result:
                result[n] = t

    # Constructor injection: `public ClassName(TypeA a, TypeB b) {`
    # Step 1: collect constructor parameter name → type
    ctor_re = re.compile(
        r'(?:public|protected)\s+' + re.escape(class_name) + r'\s*\('
    )
    cm = ctor_re.search(body[:4000])
    if cm:
        paren_end = _matching_paren(body, cm.end() - 1)
        if paren_end != -1:
            params = body[cm.end():paren_end]
            # Allow optional annotations + optional 'final' before type.
            # Handle fully qualified types (com.example.Foo) — take last segment.
            # Allow end-of-string after var name (last param has no trailing comma/paren).
            param_re = re.compile(
                r'(?:@\w+(?:\s*\([^)]*\))?\s+)*(?:final\s+)?'
                r'((?:[a-z]\w*\.)*([A-Z]\w+))(?:<[^>]*>)?\s+(\w+)\s*(?:[,)]|$)',
                re.MULTILINE,
            )
            param_to_type: dict[str, str] = {}
            for pm in param_re.finditer(params):
                # group(2) is the simple class name (last segment of qualified name)
                t, n = pm.group(2), pm.group(3)
                if t not in _JAVA_SKIP_TYPES:
                    param_to_type[n] = t

            # Step 2: scan constructor body for `this.field = param;` assignments
            # so that the actual field name (not the param name) is used in body scans
            ctor_body_start = body.find('{', paren_end)
            if ctor_body_start != -1:
                ctor_body_end = _matching_brace(body, ctor_body_start)
                ctor_body = body[ctor_body_start:ctor_body_end if ctor_body_end != -1 else ctor_body_start + 2000]
                assign_re = re.compile(r'this\.(\w+)\s*=\s*(\w+)\s*;')
                assigned_fields: set[str] = set()
                for am in assign_re.finditer(ctor_body):
                    field_n, param_n = am.group(1), am.group(2)
                    if param_n in param_to_type:
                        if field_n not in result:
                            result[field_n] = param_to_type[param_n]
                        assigned_fields.add(param_n)
                # Any param not assigned to a this.field — use param name directly
                for param_n, t in param_to_type.items():
                    if param_n not in assigned_fields and param_n not in result:
                        result[param_n] = t
            else:
                for param_n, t in param_to_type.items():
                    if param_n not in result:
                        result[param_n] = t

    return result


def _constructor_fields(text: str, class_pos: int, is_java: bool = False,
                         class_name: str = '') -> dict[str, str]:
    if is_java:
        return _java_injected_fields(text, class_name, class_pos)
    return _kotlin_constructor_fields(text, class_pos)


def _method_body(text: str, fun_start: int) -> Optional[str]:
    rest = text[fun_start:]
    bm = re.search(r'\{', rest)
    if not bm:
        return None
    end = _matching_brace(rest, bm.start())
    return rest[bm.start() + 1:end] if end != -1 else None


def _infer_kind(name: str, annotations: list[str], body_anns: list[str] = []) -> tuple[str, str]:
    """Returns (kind, reason) where reason is a human-readable explanation."""
    PRIORITY = [
        'RestController', 'FeignClient', 'KafkaListener', 'Scheduled',
        'EventListener', 'Endpoint', 'Controller', 'Repository', 'Service',
    ]
    for ann in PRIORITY:
        if ann in annotations:
            return ROLE_ANNOTATIONS[ann], f'@{ann} annotation'
    for suffix, kind in [
        ('Controller', 'CONTROLLER'),
        ('Repository', 'REPOSITORY'),
        ('Client', 'CLIENT'), ('Gateway', 'CLIENT'), ('Invoker', 'CLIENT'), ('Adapter', 'CLIENT'),
        ('Properties', 'CONFIG'), ('Props', 'CONFIG'),
        ('Validator', 'VALIDATOR'),
        ('Mapper', 'MAPPER'), ('Builder', 'MAPPER'),
        ('Cache', 'CACHE'),
        ('Scheduler', 'SCHEDULER'),
        ('Listener', 'CONSUMER'), ('Consumer', 'CONSUMER'), ('Producer', 'CONSUMER'),
        ('Service', 'SERVICE'),
        ('Facade', 'FACADE'),
        ('Handler', 'COMPONENT'),
    ]:
        if name.endswith(suffix):
            return kind, f'name ends with "{suffix}"'
    for ann in annotations:
        if ann in ROLE_ANNOTATIONS:
            return ROLE_ANNOTATIONS[ann], f'@{ann} annotation'
    for ann in body_anns:
        if ann in ROLE_ANNOTATIONS:
            return ROLE_ANNOTATIONS[ann], f'@{ann} on method (secondary)'
    return 'COMPONENT', 'no specific annotation or suffix matched'


def _infer_externals(types: list[str], supertype_snippet: str = '') -> list[str]:
    haystack = ' '.join(types) + ' ' + supertype_snippet
    found: list[str] = []
    for pattern, label in EXTERNAL_HINTS:
        if re.search(pattern, haystack) and label not in found:
            found.append(label)
    return found


def _infer_domain(package: str) -> str:
    noise = {'com', 'org', 'net', 'io', 'dev', 'app', 'main',
             'kotlin', 'java', 'service', 'services', 'api', 'web',
             'controller', 'repository', 'domain', 'model', 'dto',
             'config', 'configuration', 'util', 'utils', 'common'}
    parts = [p for p in package.split('.') if p not in noise]
    return parts[-1] if parts else ''


# ── Java AST scanner (JavaParser-backed) ─────────────────────────────────────

# Path to the pre-built fat JAR, relative to this script
_AST_JAR = Path(__file__).parent / 'ast_scanner' / 'target' / 'ast-scanner.jar'

# Cache: file path → parsed result dict (or None on error)
_ast_cache: dict[str, Optional[dict]] = {}


def _ast_scan_files(java_paths: list[Path]) -> dict[str, dict]:
    """
    Invoke the JavaParser-based scanner on a batch of .java files.
    Returns {file_path_str: result_dict}. Files that fail to parse are omitted.
    Falls back gracefully if the JAR is not found or Java is unavailable.
    """
    if not _AST_JAR.exists():
        return {}
    try:
        proc = subprocess.run(
            ['java', '-jar', str(_AST_JAR)],
            input='\n'.join(str(p) for p in java_paths),
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return {}
        results = json.loads(proc.stdout)
        return {r['file']: r for r in results if r}
    except Exception:
        return {}


def _apply_ast_result(comp: Component, ast: dict, base_path: str) -> None:
    """
    Overwrite field_map, dependencies, and endpoint call lists with AST-accurate data.
    """
    # Rebuild field_map from AST (name → type), filtering unknown types
    field_map: dict[str, str] = {}
    for f in ast.get('fields', []):
        field_map[f['name']] = f['type']
    comp.field_map = field_map
    comp.dependencies = list(dict.fromkeys(field_map.values()))

    # Update per-endpoint call lists using accurate per-method call data
    method_calls: dict[str, list[str]] = {}
    for m in ast.get('methods', []):
        called_fields = m.get('callsOnFields', [])
        # Map field name → type name
        called_types = list(dict.fromkeys(
            field_map[fn] for fn in called_fields if fn in field_map
        ))
        method_calls[m['name']] = called_types

    for ep in comp.endpoints:
        if ep.handler in method_calls:
            ep.calls = method_calls[ep.handler]


# ── File parser ───────────────────────────────────────────────────────────────

def parse_file(path: Path) -> Optional[Component]:
    path_str = str(path).replace('\\', '/')
    if any(f in path_str for f in SKIP_FRAGMENTS):
        return None

    is_java = path.suffix == '.java'
    text = path.read_text(encoding='utf-8', errors='replace')

    # Skip the Spring Boot main application class — not an architectural component
    if '@SpringBootApplication' in text:
        return None

    if is_java:
        # Skip Java enums, interfaces without Spring annotations (pure contracts),
        # and path fragments that are almost always domain objects
        if any(f in path_str for f in JAVA_SKIP_PATHS):
            # Still allow if there's a Spring annotation — it may be a @Repository etc.
            if not any(f'@{ann}' in text for ann in ROLE_ANNOTATIONS):
                return None
        class_re = JAVA_CLASS_RE
        skip_kinds_pre = re.compile(r'\benum\s+\w+')
    else:
        class_re = KT_CLASS_RE
        skip_kinds_pre = re.compile(r'\b(?:data|enum|sealed)\s*$')

    # Find primary class declaration
    for m in class_re.finditer(text):
        pre = text[max(0, m.start() - 30):m.start()]
        if skip_kinds_pre.search(pre.rstrip()):
            continue
        # Java: skip pure enums and interfaces that have no Spring annotations
        if is_java and m.group('kw') in ('enum', 'interface'):
            if not any(f'@{ann}' in text[:m.start()] for ann in ROLE_ANNOTATIONS):
                continue
        class_name = m.group('name')
        class_pos = m.start()
        break
    else:
        return None

    # Preamble: everything before the class declaration
    preamble = text[max(0, class_pos - 1000):class_pos]

    # Collect Spring annotations present in preamble
    spring_anns: list[str] = []
    for ann in {**ROLE_ANNOTATIONS, **HTTP_ANNOTATIONS}:
        if f'@{ann}' in preamble or f'@{ann}' in text[:class_pos]:
            spring_anns.append(ann)

    class_body_peek = text[class_pos:class_pos + 4000]
    body_anns: list[str] = []
    for body_ann in ('KafkaListener', 'EventListener', 'Scheduled'):
        if f'@{body_ann}' in class_body_peek and body_ann not in spring_anns:
            body_anns.append(body_ann)

    kind, classification_reason = _infer_kind(class_name, spring_anns, body_anns)

    # Skip files that don't look like Spring components at all
    has_spring = bool(spring_anns)
    has_component_name = kind not in ('COMPONENT',)
    if not has_spring and not has_component_name:
        return None

    package_m = PACKAGE_RE.search(text)
    package = package_m.group(1) if package_m else ''

    field_map = _constructor_fields(text, class_pos, is_java=is_java, class_name=class_name)
    supertype_snippet = text[class_pos:class_pos + 300]

    # External systems: from injected types + class body
    all_types = list(field_map.values())
    externals = _infer_externals(all_types, supertype_snippet + text[class_pos:class_pos + 2000])

    # @FeignClient(name = "some-service") → add that service name as external
    fc_m = re.search(r'@FeignClient\s*\(\s*(?:name\s*=\s*)?["\']([^"\']+)["\']', preamble)
    if fc_m and fc_m.group(1) not in externals:
        externals.append(fc_m.group(1))

    # If this class uses RestClient/WebClient/RestTemplate in its body (Spring 6 pattern:
    # RestClient.builder()…build() stored as a private field), derive the external system
    # name from the class name prefix rather than a generic "http" label.
    class_body = text[class_pos:class_pos + 8000]
    if REST_CLIENT_BODY_RE.search(class_body):
        for suffix in ('Client', 'Gateway', 'Adapter'):
            if class_name.endswith(suffix):
                # CamelCase → kebab-case: DistanceMatrixClient → distance-matrix
                prefix = class_name[:-len(suffix)]
                ext_name = re.sub(r'(?<=[a-z])(?=[A-Z])', '-', prefix).lower()
                if ext_name and ext_name not in externals:
                    externals.append(ext_name)
                    # Remove generic 'http' — we now have a specific system name
                    if 'http' in externals:
                        externals.remove('http')
                break
        else:
            # Not a named client class but still uses HTTP — mark generically
            if 'http' not in externals:
                externals.append('http')

    # Base path for controllers
    base_path = ''
    if kind == 'CONTROLLER':
        rm_m = re.search(r'@RequestMapping\s*\([^)]*\)', preamble)
        if rm_m:
            base_path = _annotation_path(rm_m.group(0))

    # @AppMap enrichment (optional)
    domain, capability = '', ''
    am_m = re.search(r'@AppMap\s*\(([^)]*(?:\([^)]*\)[^)]*)*)\)', preamble, re.DOTALL)
    if am_m:
        kv = am_m.group(1)
        dm = re.search(r'domain\s*=\s*"([^"]*)"', kv)
        cm_m = re.search(r'capability\s*=\s*"([^"]*)"', kv)
        ext_m = re.search(r'externalSystems\s*=\s*\[([^\]]*)\]', kv)
        if dm:
            domain = dm.group(1)
        if cm_m:
            capability = cm_m.group(1)
        if ext_m:
            appmap_exts = [s.strip().strip('"') for s in ext_m.group(1).split(',') if s.strip().strip('"')]
            for e in appmap_exts:
                if e not in externals:
                    externals.append(e)

    if not domain:
        domain = _infer_domain(package)

    comp = Component(
        name=class_name,
        kind=kind,
        package=package,
        file=path_str,
        base_path=base_path,
        field_map=field_map,
        dependencies=list(dict.fromkeys(field_map.values())),
        external_systems=externals,
        spring_annotations=spring_anns,
        classification_reason=classification_reason,
        domain=domain,
        capability=capability,
        loc=text.count('\n') + 1,
    )

    # Extract HTTP endpoints for controllers
    if kind == 'CONTROLLER':
        comp.endpoints = _extract_endpoints(text, class_pos, base_path, field_map)
    elif kind not in ('CONTROLLER',) and _HANDLER_METHOD_RE.search(text[class_pos:class_pos + 20000]):
        comp.kind = 'CONTROLLER'
        comp.endpoints = _extract_handler_endpoints(text, class_pos, field_map)

    # Extract non-HTTP entrypoints: @Scheduled, @KafkaListener, @EventListener methods
    comp.non_http_entrypoints = _extract_non_http_entrypoints(text, class_pos)

    return comp


_NON_HTTP_METHOD_RE = re.compile(
    r'@(Scheduled|KafkaListener|EventListener)\s*(?:\([^)]*\))?\s*'
    r'(?:override\s+|open\s+|internal\s+)*(?:fun|public\s+\w[\w\s]*)\s+(\w+)\s*\(',
    re.DOTALL,
)

def _extract_non_http_entrypoints(text: str, class_pos: int) -> list[NonHttpEntrypoint]:
    body = text[class_pos:class_pos + 30_000]
    result = []
    for m in _NON_HTTP_METHOD_RE.finditer(body):
        ann, method = m.group(1), m.group(2)
        # Try to extract topic/cron from annotation value
        ann_text = m.group(0)
        detail = ''
        val_m = re.search(r'\(\s*(?:topics?\s*=\s*)?["\']([^"\']+)["\']', ann_text)
        if val_m:
            detail = val_m.group(1)
        kind_map = {'Scheduled': 'SCHEDULED', 'KafkaListener': 'KAFKA', 'EventListener': 'EVENT'}
        result.append(NonHttpEntrypoint(kind=kind_map[ann], method=method, detail=detail))
    return result


def _extract_endpoints(text: str, class_pos: int, base_path: str,
                        field_map: dict[str, str]) -> list[Endpoint]:
    """Find all @XxxMapping methods inside a controller class (Kotlin + Java).

    Strategy: find method declarations first, then look *backward* for HTTP
    mapping annotations. This survives arbitrarily long @Operation / Swagger
    annotations between the mapping and the fun/method keyword.
    """
    brace_start = text.find('{', class_pos)
    if brace_start == -1:
        return []
    brace_end = _matching_brace(text, brace_start)
    body = text[brace_start:brace_end if brace_end != -1 else len(text)]

    # Build a reverse index of annotation names → http method
    ann_pattern = re.compile(
        r'@(' + '|'.join(re.escape(a) for a in HTTP_ANNOTATIONS) + r')'
        r'\s*(?P<ann_body>\((?:[^()]*|\([^()]*\))*\))?',
        re.DOTALL,
    )

    # Find all method declarations (Kotlin fun + Java public/protected)
    method_re = re.compile(
        r'(?:^|\n)[ \t]*(?:(?:override|internal|open|protected)\s+)*'
        r'(?:fun\s+(?P<kfn>\w+)|'
        r'(?:public|protected)\s+(?:(?:static|final|synchronized|@\w+\s*)*)'
        r'(?:[\w<>,\[\]? ]+?\s+)?(?P<jfn>\w+))\s*\(',
        re.MULTILINE,
    )

    seen: set[str] = set()
    previous_method_start = 0
    endpoints: list[Endpoint] = []

    for m in method_re.finditer(body):
        handler = m.group('kfn') or m.group('jfn')
        if not handler or handler in seen:
            previous_method_start = m.start()
            continue
        # Skip obvious non-handler names
        if handler in ('if', 'for', 'while', 'when', 'return', 'val', 'var',
                       'class', 'object', 'companion', 'else', 'try', 'catch'):
            previous_method_start = m.start()
            continue

        # Look backward only to the previous method declaration. This allows
        # Swagger/OpenAPI annotations between the mapping and method, but
        # prevents reusing one mapping annotation for later helper methods.
        lookback_start = previous_method_start
        lookback = body[lookback_start:m.start()]

        http_method: Optional[str] = None
        ann_body_str = ''
        # Find the LAST mapping annotation in the lookback window
        for am in ann_pattern.finditer(lookback):
            ann_name = am.group(1)
            if ann_name in HTTP_ANNOTATIONS:
                http_method = HTTP_ANNOTATIONS[ann_name]
                ann_body_str = am.group('ann_body') or ''

        if http_method is None:
            previous_method_start = m.start()
            continue

        seen.add(handler)
        local_path = _annotation_path(ann_body_str)
        full_path = (base_path.rstrip('/') + '/' + local_path.lstrip('/')).rstrip('/')
        if not full_path:
            full_path = '/'

        mb = _method_body(body, m.start())
        calls = list(dict.fromkeys(_field_calls_recursive(mb, field_map, body, {handler}))) if mb else []

        endpoints.append(Endpoint(
            http_method=http_method,
            path=full_path,
            handler=handler,
            calls=calls,
        ))
        previous_method_start = m.start()

    return endpoints


def _field_calls_recursive(mb: str, field_map: dict[str, str], class_body: str,
                            visited: set[str]) -> list[str]:
    """Return field type names called in mb, following private Kotlin helper methods."""
    found: list[str] = []
    for field_name, type_name in field_map.items():
        if re.search(rf'(?:this\.)?{re.escape(field_name)}\s*[.(]', mb):
            found.append(type_name)
    # Follow private fun helpers called from this body
    for pm in re.finditer(r'private\s+fun\s+(\w+)\s*\(', class_body):
        helper = pm.group(1)
        if helper in visited:
            continue
        if re.search(rf'\b{re.escape(helper)}\s*\(', mb):
            bs = class_body.find('{', pm.end())
            if bs == -1:
                continue
            be = _matching_brace(class_body, bs)
            helper_body = class_body[bs: be if be != -1 else bs + 4000]
            visited.add(helper)
            found.extend(_field_calls_recursive(helper_body, field_map, class_body, visited))
    return found


# ── Scanner ───────────────────────────────────────────────────────────────────

def _parse_router_operations(text: str) -> dict[str, dict[str, dict]]:
    """
    Parse @RouterOperations to extract endpoint metadata from a functional router config.
    Returns: {HandlerClassName: {methodName: {method, path}}}
    """
    result: dict[str, dict[str, dict]] = defaultdict(dict)
    # Each RouterOperation(...) block — handle nested parens by matching greedily up to beanClass
    ro_re = re.compile(r'RouterOperation\s*\(([^)]{0,2000}?\bpath\s*=[^)]{0,500}?)\)', re.DOTALL)
    for m in ro_re.finditer(text):
        op = m.group(1)
        method_m = re.search(r'method\s*=\s*\[RequestMethod\.(\w+)\]', op)
        path_m   = re.search(r'path\s*=\s*"([^"]+)"', op)
        bean_m   = re.search(r'beanClass\s*=\s*(\w+)::class', op)
        bm_m     = re.search(r'beanMethod\s*=\s*"([^"]+)"', op)
        if path_m and bean_m and bm_m:
            http_method = method_m.group(1) if method_m else 'GET'
            result[bean_m.group(1)][bm_m.group(1)] = {
                'method': http_method,
                'path': path_m.group(1),
            }
    return dict(result)


# Matches functional handler methods: fun name(request: ServerRequest): ServerResponse
_HANDLER_METHOD_RE = re.compile(
    r'fun\s+(\w+)\s*\(\s*\w+\s*:\s*ServerRequest\s*\)\s*:\s*ServerResponse'
)


def _extract_handler_endpoints(text: str, class_pos: int,
                                field_map: dict[str, str]) -> list[Endpoint]:
    """
    Extract endpoints from a functional handler class.
    Paths are placeholder method names; scan() replaces them from the Routes file.
    Follows private helper methods called from the handler body.
    """
    body = text[class_pos:]

    # Pre-index all private method bodies so we can follow delegation chains
    _PRIVATE_FUN_RE = re.compile(r'private\s+fun\s+(\w+)\s*\(')
    private_bodies: dict[str, str] = {}
    for pm in _PRIVATE_FUN_RE.finditer(body):
        bs = body.find('{', pm.end())
        if bs == -1:
            continue
        be = _matching_brace(body, bs)
        private_bodies[pm.group(1)] = body[bs: be if be != -1 else bs + 4000]

    endpoints: list[Endpoint] = []
    for m in _HANDLER_METHOD_RE.finditer(body):
        fn_name = m.group(1)
        pre = body[max(0, m.start()-50):m.start()]
        if 'private' in pre:
            continue
        brace_start = body.find('{', m.end())
        if brace_start == -1:
            continue
        brace_end = _matching_brace(body, brace_start)
        mb = body[brace_start: brace_end if brace_end != -1 else brace_start + 4000]
        calls = list(dict.fromkeys(_field_calls_recursive(mb, field_map, body, {fn_name})))
        endpoints.append(Endpoint(http_method='GET', path=f'__handler__{fn_name}', handler=fn_name, calls=calls))
    return endpoints


def scan(root: Path) -> tuple[list[Component], list[str], int]:
    """Returns (components, warnings, ast_enriched_count)."""
    components: list[Component] = []
    warnings: list[str] = []
    parse_errors = 0
    all_files = sorted(root.rglob('*.kt')) + sorted(root.rglob('*.java'))
    for src_file in sorted(all_files, key=lambda p: str(p)):
        try:
            comp = parse_file(src_file)
            if comp:
                components.append(comp)
        except Exception as e:
            parse_errors += 1
            warnings.append(f'Could not parse {src_file.name}: {e}')

    if parse_errors:
        warnings.append(f'{parse_errors} file(s) could not be parsed — regex fallback used')

    # Warn on ambiguous roles — skip expected meta-annotation pairs
    # (@Configuration is always meta-annotated with @Component, @Endpoint likewise)
    _META_IMPLIES_COMPONENT = {'Configuration', 'Endpoint', 'RestController', 'Controller',
                               'Repository', 'Service'}
    for c in components:
        role_anns = [a for a in c.spring_annotations if a in ROLE_ANNOTATIONS]
        if len(role_anns) > 1:
            real_ambiguous = [a for a in role_anns
                              if a != 'Component' or not any(r in _META_IMPLIES_COMPONENT for r in role_anns)]
            if len(real_ambiguous) > 1:
                warnings.append(f'Ambiguous role for {c.name}: {", ".join("@"+a for a in role_anns)} → classified as {c.kind}')

    # AST pass: re-scan Java files with JavaParser for accurate field types + method calls
    java_comps = [c for c in components if c.file.endswith('.java')]
    ast_enriched = 0
    if java_comps and _AST_JAR.exists():
        print('Running AST scanner on Java files…', file=sys.stderr)
        ast_results = _ast_scan_files([Path(c.file) for c in java_comps])
        if ast_results:
            for comp in java_comps:
                ast = ast_results.get(comp.file)
                if ast:
                    _apply_ast_result(comp, ast, comp.base_path)
            ast_enriched = len(ast_results)
            print(f'  AST: enriched {ast_enriched} Java components.', file=sys.stderr)
    elif java_comps:
        warnings.append('Java AST scanner JAR not found — using regex fallback for Java files')

    # Second pass: find router config files, extract RouterOperations, and attach
    # real paths to handler components that were detected with placeholder paths.
    router_ops: dict[str, dict[str, dict]] = {}
    for src_file in all_files:
        text = src_file.read_text(encoding='utf-8', errors='replace')
        if 'RouterFunction' in text and 'RouterOperation' in text:
            ops = _parse_router_operations(text)
            for handler_cls, methods in ops.items():
                router_ops.setdefault(handler_cls, {}).update(methods)

    if router_ops:
        for comp in components:
            if comp.name not in router_ops:
                continue
            ops = router_ops[comp.name]
            updated: list[Endpoint] = []
            for ep in comp.endpoints:
                if ep.path.startswith('__handler__'):
                    fn_name = ep.path[len('__handler__'):]
                    if fn_name in ops:
                        info = ops[fn_name]
                        updated.append(Endpoint(http_method=info['method'], path=info['path'], handler=fn_name, calls=ep.calls))
                    # drop endpoints with no matching route (private helpers etc.)
                else:
                    updated.append(ep)
            comp.endpoints = updated

    return components, warnings, ast_enriched


def resolve(components: list[Component]) -> None:
    """Trim dependency lists to only reference other known components.
    Also resolves interface names to their implementations via FooImpl heuristic.
    Unresolvable dependencies that look like external clients are added as external systems."""
    known = {c.name for c in components}

    # Suffixes that suggest an external service boundary rather than a utility type
    _EXTERNAL_SUFFIXES = ('Service', 'Client', 'Gateway', 'Adapter', 'Api', 'Provider')
    _NOISE_TYPES = frozenset({
        'String', 'Integer', 'Long', 'Boolean', 'List', 'Map', 'Set', 'Optional',
        'ObjectMapper', 'Logger', 'Duration', 'Cache', 'AtomicBoolean', 'BiConsumer',
    })
    # Known library/framework types → canonical external system label
    _KNOWN_TYPES: dict[str, str] = {
        'CloseableHttpClient': 'http',
        'HttpClient': 'http',
        'OkHttpClient': 'http',
        'RestTemplate': 'http',
        'WebClient': 'http',
        'RestClient': 'http',
        'ElasticsearchClient': 'elasticsearch',
        'RestHighLevelClient': 'elasticsearch',
        'OpenSearchClient': 'opensearch',
    }
    # Prefixes to strip before label generation (project-specific noise)
    _LABEL_DROP_PREFIXES = ('API', 'Api', 'Monthly')

    def _to_external_label(name: str) -> Optional[str]:
        """Convert unresolved type name to a kebab-case external system label, or None."""
        if name in _NOISE_TYPES:
            return None
        if name in _KNOWN_TYPES:
            return _KNOWN_TYPES[name]
        for suffix in _EXTERNAL_SUFFIXES:
            if name.endswith(suffix):
                prefix = name[:-len(suffix)]
                if not prefix:
                    return None
                # Strip known noise prefixes
                for dp in _LABEL_DROP_PREFIXES:
                    if prefix.startswith(dp) and len(prefix) > len(dp):
                        prefix = prefix[len(dp):]
                label = re.sub(r'(?<=[a-z])(?=[A-Z])', '-', prefix).lower()
                # Reject labels that are too long (>3 words = likely an internal class name)
                if label.count('-') >= 3:
                    return None
                return label
        return None

    def _resolve_dep(dep: str) -> str:
        if dep in known:
            return dep
        for suffix in ('Impl', 'Implementation'):
            candidate = dep + suffix
            if candidate in known:
                return candidate
        return dep

    for comp in components:
        resolved = []
        for d in comp.dependencies:
            r = _resolve_dep(d)
            if r in known and r != comp.name:
                resolved.append(r)
            elif r not in known:
                label = _to_external_label(d)
                if label and label not in comp.external_systems:
                    comp.external_systems.append(label)
        comp.dependencies = list(dict.fromkeys(resolved))

        for ep in comp.endpoints:
            ep.calls = [c for c in ep.calls if c in known]


# ── HTML generator ────────────────────────────────────────────────────────────

# bg=dark-mode background, border=accent, font=dark-mode text
# lbg=light-mode background, lfont=light-mode text
KIND_VIS: dict[str, dict] = {
    'CONTROLLER': {'bg': '#1d4ed8', 'border': '#60a5fa', 'font': '#ffffff', 'lbg': '#dbeafe', 'lfont': '#1e3a8a', 'shape': 'box'},
    'SERVICE':    {'bg': '#15803d', 'border': '#4ade80', 'font': '#ffffff', 'lbg': '#dcfce7', 'lfont': '#14532d', 'shape': 'box'},
    'REPOSITORY': {'bg': '#7e22ce', 'border': '#c084fc', 'font': '#ffffff', 'lbg': '#f3e8ff', 'lfont': '#581c87', 'shape': 'box'},
    'CLIENT':     {'bg': '#b45309', 'border': '#fbbf24', 'font': '#ffffff', 'lbg': '#fef3c7', 'lfont': '#78350f', 'shape': 'box'},
    'CONSUMER':   {'bg': '#0f766e', 'border': '#2dd4bf', 'font': '#ffffff', 'lbg': '#ccfbf1', 'lfont': '#134e4a', 'shape': 'box'},
    'GATEWAY':    {'bg': '#be123c', 'border': '#fb7185', 'font': '#ffffff', 'lbg': '#ffe4e6', 'lfont': '#881337', 'shape': 'box'},
    'SCHEDULER':  {'bg': '#374151', 'border': '#9ca3af', 'font': '#f3f4f6', 'lbg': '#f1f5f9', 'lfont': '#374151', 'shape': 'box'},
    'MAPPER':     {'bg': '#0e7490', 'border': '#22d3ee', 'font': '#ffffff', 'lbg': '#cffafe', 'lfont': '#083344', 'shape': 'box'},
    'VALIDATOR':  {'bg': '#a16207', 'border': '#facc15', 'font': '#ffffff', 'lbg': '#fef9c3', 'lfont': '#713f12', 'shape': 'box'},
    'FACADE':     {'bg': '#1d4ed8', 'border': '#93c5fd', 'font': '#ffffff', 'lbg': '#eff6ff', 'lfont': '#1e3a8a', 'shape': 'box'},
    'LISTENER':   {'bg': '#4338ca', 'border': '#a5b4fc', 'font': '#ffffff', 'lbg': '#e0e7ff', 'lfont': '#312e81', 'shape': 'box'},
    'CACHE':      {'bg': '#065f46', 'border': '#34d399', 'font': '#ffffff', 'lbg': '#d1fae5', 'lfont': '#064e3b', 'shape': 'box'},
    'COMPONENT':  {'bg': '#334155', 'border': '#64748b', 'font': '#e2e8f0', 'lbg': '#f1f5f9', 'lfont': '#334155', 'shape': 'box'},
    'CONFIG':     {'bg': '#292524', 'border': '#78716c', 'font': '#d6d3d1', 'lbg': '#f5f5f4', 'lfont': '#44403c', 'shape': 'box'},
}
EXT_VIS = {'bg': '#1e293b', 'border': '#64748b', 'font': '#94a3b8', 'lbg': '#e2e8f0', 'lfont': '#475569', 'shape': 'ellipse'}

# Lane order for hierarchical LR layout: Controller → Service → Client → Repository → External
KIND_LEVEL: dict[str, int] = {
    'CONTROLLER': 0,
    'SERVICE': 1, 'FACADE': 1, 'SCHEDULER': 1, 'CONSUMER': 1, 'LISTENER': 1, 'COMPONENT': 1, 'CONFIG': 1,
    'CLIENT': 2, 'GATEWAY': 2, 'MAPPER': 2, 'VALIDATOR': 2, 'CACHE': 2,
    'REPOSITORY': 3,
}


def _build_graph_data(components: list[Component]) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    edges: list[dict] = []
    id_map: dict[str, int] = {}
    nid = 0

    def gid(key: str) -> int:
        nonlocal nid
        if key not in id_map:
            id_map[key] = nid
            nid += 1
        return id_map[key]

    for comp in components:
        vis = KIND_VIS.get(comp.kind, KIND_VIS['COMPONENT'])
        nodes.append({
            'id': gid(comp.name),
            'label': comp.name,
            'level': KIND_LEVEL.get(comp.kind, 1),
            'color': {
                'background': vis['bg'],
                'border': vis['border'],
                'highlight': {'background': vis['border'], 'border': '#ffffff'},
            },
            'font': {'color': vis['font'], 'size': 13, 'face': 'system-ui'},
            'shape': vis['shape'],
            'shadow': {'enabled': True, 'color': 'rgba(0,0,0,0.2)', 'size': 4},
            '_name': comp.name,
            '_kind': comp.kind,
            '_domain': comp.domain,
            '_dark': {'bg': vis['bg'], 'font': vis['font']},
            '_light': {'bg': vis['lbg'], 'font': vis['lfont']},
            '_border': vis['border'],
        })

    # Deduplicate external systems
    ext_callers: dict[str, list[str]] = defaultdict(list)
    for comp in components:
        for ext in comp.external_systems:
            if comp.name not in ext_callers[ext]:
                ext_callers[ext].append(comp.name)

    for ext_name in ext_callers:
        nodes.append({
            'id': gid(f'__ext__{ext_name}'),
            'label': ext_name,
            'level': 4,
            'color': {
                'background': EXT_VIS['bg'],
                'border': EXT_VIS['border'],
                'highlight': {'background': '#374151', 'border': '#9ca3af'},
            },
            'font': {'color': EXT_VIS['font'], 'size': 12, 'face': 'system-ui'},
            'shape': EXT_VIS['shape'],
            '_name': ext_name,
            '_kind': 'EXTERNAL',
            '_dark': {'bg': EXT_VIS['bg'], 'font': EXT_VIS['font']},
            '_light': {'bg': EXT_VIS['lbg'], 'font': EXT_VIS['lfont']},
            '_border': EXT_VIS['border'],
        })

    # Component → component edges
    for comp in components:
        for dep in comp.dependencies:
            edges.append({
                'from': gid(comp.name), 'to': gid(dep),
                '_fn': comp.name, '_tn': dep,
            })

    # Component → external edges (dashed)
    for comp in components:
        for ext in comp.external_systems:
            edges.append({
                'from': gid(comp.name), 'to': gid(f'__ext__{ext}'),
                'dashes': True,
                '_fn': comp.name, '_tn': ext,
            })

    return nodes, edges


def _sidebar_data(components: list[Component]) -> list[dict]:
    result = []
    for comp in sorted(components, key=lambda c: c.name):
        has_http = comp.kind == 'CONTROLLER' and comp.endpoints
        has_non_http = bool(comp.non_http_entrypoints)
        if not has_http and not has_non_http:
            continue
        result.append({
            'controller': comp.name,
            'domain': comp.domain,
            'kind': comp.kind,
            'endpoints': [
                {
                    'method': ep.http_method,
                    'path': ep.path,
                    'handler': ep.handler,
                    'calls': ep.calls,
                }
                for ep in sorted(comp.endpoints, key=lambda e: (e.path, e.http_method))
            ] if has_http else [],
            'nonHttpEntrypoints': [
                {'kind': e.kind, 'method': e.method, 'detail': e.detail}
                for e in comp.non_http_entrypoints
            ],
        })
    return result


def _comp_lookup(components: list[Component]) -> dict:
    lut = {}
    for c in components:
        lut[c.name] = {
            'name': c.name,
            'kind': c.kind,
            'package': c.package,
            'domain': c.domain,
            'capability': c.capability,
            'dependencies': c.dependencies,
            'externalSystems': c.external_systems,
            'springAnnotations': c.spring_annotations,
            'classificationReason': c.classification_reason,
            'file': c.file,
            'loc': c.loc,
            'nonHttpEntrypoints': [
                {'kind': e.kind, 'method': e.method, 'detail': e.detail}
                for e in c.non_http_entrypoints
            ],
            'endpoints': [
                {'method': ep.http_method, 'path': ep.path, 'handler': ep.handler, 'calls': ep.calls}
                for ep in c.endpoints
            ],
        }
    return lut


def _git_meta(root: Path) -> dict:
    import subprocess as _sp2
    meta = {}
    try:
        meta['branch'] = _sp2.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=root, capture_output=True, text=True, timeout=5
        ).stdout.strip()
        meta['commit'] = _sp2.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=root, capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except Exception:
        pass
    return meta


_SCANNER_VERSION = '1.3.0'


def generate_html(components: list[Component], title: str = 'Application Map',
                  scan_root: Path | None = None, warnings: list[str] | None = None,
                  ast_enriched: int = 0) -> str:
    nodes, edges = _build_graph_data(components)
    sidebar = _sidebar_data(components)
    comp_lut = _comp_lookup(components)

    import datetime as _dt
    git = _git_meta(scan_root) if scan_root else {}
    scan_meta = {
        'root': str(scan_root) if scan_root else '',
        'branch': git.get('branch', ''),
        'commit': git.get('commit', ''),
        'timestamp': _dt.datetime.now().isoformat(timespec='seconds'),
        'scannerVersion': _SCANNER_VERSION,
        'astEnriched': ast_enriched,
        'totalComponents': len(components),
    }
    warnings = warnings or []

    total_ep = sum(len(c.endpoints) for c in components if c.kind == 'CONTROLLER')
    n_ctrl = sum(1 for c in components if c.kind == 'CONTROLLER')
    n_svc  = sum(1 for c in components if c.kind == 'SERVICE')
    n_repo = sum(1 for c in components if c.kind == 'REPOSITORY')
    n_cli  = sum(1 for c in components if c.kind == 'CLIENT')

    stats = f'{n_ctrl} controllers · {n_svc} services · {n_repo} repos · {n_cli} clients · {total_ep} endpoints'

    html = HTML_TEMPLATE
    html = html.replace('{{TITLE}}', _html_escape(title))
    html = html.replace('{{STATS}}', _html_escape(stats))
    html = html.replace('{{GRAPH_NODES}}', _json_for_script(nodes))
    html = html.replace('{{GRAPH_EDGES}}', _json_for_script(edges))
    html = html.replace('{{SIDEBAR_DATA}}', _json_for_script(sidebar))
    html = html.replace('{{COMP_DATA}}', _json_for_script(comp_lut))
    html = html.replace('{{SCAN_META}}', _json_for_script(scan_meta))
    html = html.replace('{{WARNINGS}}', _json_for_script(warnings))
    return html


# ── Markdown / Mermaid generator ──────────────────────────────────────────────

MERMAID_KIND_STYLE: dict[str, str] = {
    'CONTROLLER': 'fill:#1e3a8a,stroke:#3b82f6,color:#e0f2fe',
    'SERVICE':    'fill:#14532d,stroke:#22c55e,color:#dcfce7',
    'REPOSITORY': 'fill:#3b0764,stroke:#a855f7,color:#f3e8ff',
    'CLIENT':     'fill:#78350f,stroke:#f59e0b,color:#fef3c7',
    'CONSUMER':   'fill:#134e4a,stroke:#14b8a6,color:#ccfbf1',
    'MAPPER':     'fill:#083344,stroke:#22d3ee,color:#cffafe',
    'SCHEDULER':  'fill:#1f2937,stroke:#6b7280,color:#e5e7eb',
}

MERMAID_SHAPE: dict[str, tuple[str, str]] = {
    'CONTROLLER': ('(', ')'),
    'SERVICE':    ('[', ']'),
    'REPOSITORY': ('[(', ')]'),
    'CLIENT':     ('>', ']'),
    'CONSUMER':   ('[/', '/]'),
    'MAPPER':     ('{', '}'),
}


def _nid(name: str) -> str:
    return re.sub(r'\W', '_', name)


def _endpoint_chain(ctrl: Component, by_name: dict[str, Component]) -> list[Component]:
    """BFS from a controller through its dependencies, returning ordered unique components."""
    seen: set[str] = set()
    queue = [ctrl]
    result: list[Component] = []
    while queue:
        c = queue.pop(0)
        if c.name in seen:
            continue
        seen.add(c.name)
        result.append(c)
        for dep in c.dependencies:
            if dep in by_name and dep not in seen:
                queue.append(by_name[dep])
    return result


def _slug(method: str, path: str) -> str:
    safe = re.sub(r'[^a-zA-Z0-9]+', '-', f'{method}-{path}').strip('-').lower()
    return safe


def generate_endpoint_docs(components: list[Component], output_dir: Path, title: str = '') -> int:
    """Write one markdown file per HTTP endpoint into output_dir. Returns file count."""
    by_name = {c.name: c for c in components}
    output_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    controllers = [c for c in components if c.endpoints]
    for ctrl in controllers:
        chain_comps = _endpoint_chain(ctrl, by_name)
        for ep in ctrl.endpoints:
            lines: list[str] = []

            # Header
            lines += [f'# {ep.http_method} {ep.path}', '']
            lines += [f'**Handler:** `{ep.handler}()`  ']
            lines += [f'**Controller:** `{ctrl.name}`' + (f' · domain: `{ctrl.domain}`' if ctrl.domain else ''), '']

            # Mermaid call chain
            mermaid_nodes: list[str] = []
            mermaid_edges: list[str] = []
            seen_edges: set[tuple[str, str]] = set()
            for comp in chain_comps:
                label = comp.name
                shape = {
                    'CONTROLLER': f'("{label}")',
                    'REPOSITORY': f'[("{label}")]',
                    'CLIENT':     f'["{label}"]',
                    'CONSUMER':   f'["{label}"]',
                }.get(comp.kind, f'["{label}"]')
                mermaid_nodes.append(f'  {comp.name}{shape}')
                for dep in comp.dependencies:
                    if dep in by_name and (comp.name, dep) not in seen_edges:
                        seen_edges.add((comp.name, dep))
                        mermaid_edges.append(f'  {comp.name} --> {dep}')
                for ext in comp.external_systems:
                    ext_id = re.sub(r'[^a-zA-Z0-9]', '_', ext)
                    edge = (comp.name, ext_id)
                    if edge not in seen_edges:
                        seen_edges.add(edge)
                        mermaid_nodes.append(f'  {ext_id}["{ext}"]:::ext')
                        mermaid_edges.append(f'  {comp.name} -.-> {ext_id}')

            lines += ['## Call chain', '']
            lines += ['```mermaid', 'graph LR']
            lines += mermaid_nodes
            lines += mermaid_edges
            lines += ['```', '']

            # Components table
            lines += ['## Components', '']
            lines += ['| Component | Kind | Domain | File |']
            lines += ['|---|---|---|---|']
            for comp in chain_comps:
                short_file = re.sub(r'.*/src/main/(kotlin|java)/', '', comp.file)
                domain = comp.domain or '—'
                kind = comp.kind.capitalize()
                lines.append(f'| `{comp.name}` | {kind} | {domain} | `{short_file}` |')
            lines.append('')

            # External systems
            all_ext: dict[str, list[str]] = {}
            for comp in chain_comps:
                for ext in comp.external_systems:
                    all_ext.setdefault(ext, []).append(comp.name)
            if all_ext:
                lines += ['## External systems', '']
                for ext, callers in all_ext.items():
                    caller_str = ', '.join(f'`{c}`' for c in callers)
                    lines.append(f'- **{ext}** — called by {caller_str}')
                lines.append('')

            # Write file
            slug = _slug(ep.http_method, ep.path)
            out_path = output_dir / f'{slug}.md'
            out_path.write_text('\n'.join(lines), encoding='utf-8')
            written += 1

    return written


def generate_markdown(components: list[Component]) -> str:
    domains = sorted({c.domain for c in components if c.domain})
    lines: list[str] = []

    lines += [
        '# Application Architecture',
        '',
        f'> Generated by **springmap**. '
        f'{len(components)} components across {len(domains)} domain(s).',
        '',
    ]

    # ── Overview ──
    lines += ['## Component Overview', '', '```mermaid', 'graph TD']

    domain_groups: dict[str, list[Component]] = defaultdict(list)
    for c in components:
        domain_groups[c.domain or '_'].append(c)

    for domain, members in sorted(domain_groups.items()):
        safe = _nid(domain)
        title = domain.title() if domain != '_' else 'Other'
        lines.append(f'  subgraph {safe}["{title}"]')
        for comp in members:
            o, c = MERMAID_SHAPE.get(comp.kind, ('[', ']'))
            label = comp.capability or comp.name
            lines.append(f'    {_nid(comp.name)}{o}"{comp.name}\\n{label}"{c}')
        lines.append('  end')

    lines.append('')
    known = {c.name for c in components}
    for comp in components:
        for dep in comp.dependencies:
            if dep in known:
                lines.append(f'  {_nid(comp.name)} --> {_nid(dep)}')

    lines.append('')
    for comp in components:
        style = MERMAID_KIND_STYLE.get(comp.kind, 'fill:#1e293b,color:#cbd5e1')
        lines.append(f'  style {_nid(comp.name)} {style}')

    lines += ['```', '']

    # ── External systems ──
    ext_map: dict[str, list[str]] = defaultdict(list)
    for comp in components:
        for ext in comp.external_systems:
            if comp.name not in ext_map[ext]:
                ext_map[ext].append(comp.name)

    if ext_map:
        lines += ['## External Systems', '', '```mermaid', 'graph LR']
        for ext, callers in sorted(ext_map.items()):
            eid = _nid(ext)
            lines.append(f'  {eid}[("{ext}")]')
            lines.append(f'  style {eid} fill:#111827,stroke:#374151,color:#9ca3af')
            for caller in callers:
                lines.append(f'  {_nid(caller)} --> {eid}')
        lines += ['```', '']

    # ── Endpoints ──
    controllers = [c for c in components if c.kind == 'CONTROLLER' and c.endpoints]
    if controllers:
        lines += ['## HTTP Endpoints', '']
        for ctrl in sorted(controllers, key=lambda c: c.name):
            lines.append(f'### `{ctrl.name}`')
            lines.append('')
            lines.append('| Method | Path | Handler | Calls |')
            lines.append('|---|---|---|---|')
            for ep in sorted(ctrl.endpoints, key=lambda e: (e.path, e.http_method)):
                calls = ', '.join(f'`{s}`' for s in ep.calls) or '—'
                lines.append(f'| `{ep.http_method}` | `{ep.path}` | `{ep.handler}()` | {calls} |')
            lines.append('')

    # ── Per-domain ──
    if domains:
        lines += ['## Domain Detail', '']
        for domain in domains:
            members = [c for c in components if c.domain == domain]
            lines.append(f'### {domain.title()}')
            lines.append('')
            lines.append('| Component | Kind | Dependencies | External |')
            lines.append('|---|---|---|---|')
            for comp in sorted(members, key=lambda c: (c.kind, c.name)):
                deps = ', '.join(f'`{d}`' for d in comp.dependencies) or '—'
                ext  = ', '.join(f'`{e}`' for e in comp.external_systems) or '—'
                lines.append(f'| `{comp.name}` | {comp.kind.title()} | {deps} | {ext} |')
            lines.append('')

    return '\n'.join(lines)


# ── HTML template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{{TITLE}}</title>
<style>
:root{
  --bg:#0b1120;--surface:#111827;--border:#1f2937;--text:#e2e8f0;
  --text-dim:#6b7280;--text-muted:#475569;--text-code:#94a3b8;
  --hover:#0b1120;--active-outline:#1d4ed8;--chip-bg:#172554;
  --chip-border:#1e3a8a;--chip-text:#60a5fa;--input-bg:#0b1120;
  --scrollbar:#1f2937;--detail-label:#4b5563;--detail-val:#cbd5e1;
  --arrow:#374151;
}
body.light{
  --bg:#f1f5f9;--surface:#ffffff;--border:#e2e8f0;--text:#0f172a;
  --text-dim:#64748b;--text-muted:#94a3b8;--text-code:#334155;
  --hover:#f8fafc;--active-outline:#2563eb;--chip-bg:#dbeafe;
  --chip-border:#93c5fd;--chip-text:#1d4ed8;--input-bg:#f8fafc;
  --scrollbar:#cbd5e1;--detail-label:#94a3b8;--detail-val:#334155;
  --arrow:#cbd5e1;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
     background:var(--bg);color:var(--text);height:100vh;display:flex;
     flex-direction:column;overflow:hidden}
header{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 18px;
       display:flex;align-items:center;gap:12px;flex-shrink:0;z-index:10}
header svg{flex-shrink:0}
header h1{font-size:15px;font-weight:600;color:var(--text)}
.stats{font-size:11px;color:var(--text-dim);margin-left:auto}
.layout{display:flex;flex:1;overflow:hidden}

/* Sidebar */
.sidebar{width:290px;background:var(--surface);border-right:1px solid var(--border);
         display:flex;flex-direction:column;overflow:hidden;flex-shrink:0}
.sb-tabs{display:flex;border-bottom:1px solid var(--border);background:var(--surface);flex-shrink:0}
.sb-tab{flex:1;padding:7px 2px;font-size:10px;font-weight:600;text-align:center;
  cursor:pointer;color:var(--text-muted);border-bottom:2px solid transparent;
  transition:all .15s;user-select:none;white-space:nowrap}
.sb-tab:hover{color:var(--text)}
.sb-tab.active{color:var(--text);border-bottom-color:#3b82f6}
.sb-search-wrap{padding:10px;border-bottom:1px solid var(--border);display:none}
.sb-search-wrap.visible{display:block}
.sb-search-wrap input{width:100%;background:var(--input-bg);border:1px solid var(--border);
  border-radius:6px;padding:6px 10px;color:var(--text);font-size:12px;outline:none}
.sb-search-wrap input:focus{border-color:#3b82f6}
.search-result{padding:6px 12px;cursor:pointer;transition:background .1s;border-bottom:1px solid var(--border)}
.search-result:hover{background:var(--hover)}
.search-result-kind{font-size:9px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em}
.search-result-name{font-size:12px;font-weight:600;color:var(--text)}
.search-result-sub{font-size:11px;color:var(--text-muted);font-family:'SF Mono','Fira Code',monospace;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sidebar-body{flex:1;overflow-y:auto;padding:4px 0}
.ctrl-group{margin-bottom:2px}
.ctrl-header{padding:7px 12px 4px;font-size:11px;font-weight:700;color:var(--text-dim);
  cursor:pointer;display:flex;align-items:center;gap:6px;user-select:none;transition:color .15s}
.ctrl-header:hover{color:var(--text)}
.domain-chip{background:var(--chip-bg);border:1px solid var(--chip-border);border-radius:3px;
  padding:1px 5px;font-size:9px;color:var(--chip-text);font-weight:600;
  text-transform:none;letter-spacing:0}
.ep-list{padding:0 8px 6px}
.ep-item{display:flex;align-items:center;gap:7px;padding:5px 8px;
  border-radius:5px;cursor:pointer;transition:background .1s;user-select:none;min-width:0}
.ep-item:hover{background:var(--hover)}
.ep-item.active{background:var(--hover);outline:1px solid var(--active-outline)}
.ep-path{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.method{font-size:9px;font-weight:800;padding:2px 5px;border-radius:3px;
  min-width:40px;text-align:center;flex-shrink:0;margin-top:1px;letter-spacing:.02em}
.m-GET{background:#052e16;color:#4ade80}
.m-POST{background:#0f1d3d;color:#60a5fa}
.m-PUT{background:#2c1700;color:#fbbf24}
.m-DELETE{background:#2c0000;color:#f87171}
.m-PATCH{background:#1a0033;color:#c084fc}
.m-ANY{background:#1e293b;color:#64748b}
body.light .m-GET{background:#dcfce7;color:#166534}
body.light .m-POST{background:#dbeafe;color:#1e40af}
body.light .m-PUT{background:#fef3c7;color:#92400e}
body.light .m-DELETE{background:#fee2e2;color:#991b1b}
body.light .m-PATCH{background:#f3e8ff;color:#6b21a8}
body.light .m-ANY{background:#f1f5f9;color:#64748b}
.ep-path{font-family:'SF Mono','Fira Code',monospace;font-size:11px;
  color:var(--text-code);word-break:break-all;line-height:1.4}

/* Legend */
.legend{padding:8px 12px;border-top:1px solid var(--border);font-size:10px;
  color:var(--text-muted);display:flex;flex-wrap:wrap;gap:6px}
.legend-item{display:flex;align-items:center;gap:4px}
.legend-dot{width:8px;height:8px;border-radius:2px;flex-shrink:0}

/* Main */
.main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
#chain-area{flex:1;min-height:0;overflow:auto;position:relative;background:var(--bg)}
#chain-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none}
#chain-empty-inner{display:flex;flex-direction:column;align-items:center;gap:12px;opacity:.3}
#chain-empty-inner svg{color:var(--text)}
#chain-empty-inner p{font-size:14px;color:var(--text);margin:0}
#chain-empty.hidden{display:none}
#chain-svg{padding:40px 48px;display:inline-block;min-width:100%}

/* Detail panel */
.detail{background:var(--surface);border-top:1px solid var(--border);padding:14px 20px;
  flex-shrink:0;font-size:13px;max-height:220px;overflow-y:auto;transition:all .2s}
.detail.hidden{display:none}
.detail h3{font-size:14px;font-weight:600;color:var(--text);margin-bottom:10px;
  display:flex;align-items:center;gap:8px}
.detail-grid{display:flex;flex-wrap:wrap;gap:16px}
.df label{font-size:10px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--detail-label);display:block;margin-bottom:2px}
.df .v{color:var(--detail-val);font-family:'SF Mono','Fira Code',monospace;font-size:11px}
.chain{font-size:12px;line-height:1.7}
.chain-row{display:flex;align-items:center;gap:8px;padding:1px 0}
.arrow{color:var(--arrow);font-size:10px}
.kbadge{font-size:9px;font-weight:700;padding:2px 5px;border-radius:3px}
.k-SERVICE{background:#052e16;color:#4ade80}
.k-REPOSITORY{background:#1a0033;color:#c084fc}
.k-CLIENT{background:#2c1700;color:#fbbf24}
.k-EXTERNAL{background:#111827;color:#6b7280;border:1px solid #374151}
.k-CONSUMER{background:#022c22;color:#5eead4}
.k-MAPPER{background:#0c1e24;color:#22d3ee}
body.light .k-SERVICE{background:#dcfce7;color:#166534}
body.light .k-REPOSITORY{background:#f3e8ff;color:#6b21a8}
body.light .k-CLIENT{background:#fef3c7;color:#92400e}
body.light .k-EXTERNAL{background:#f1f5f9;color:#64748b;border-color:#e2e8f0}
body.light .k-CONSUMER{background:#ccfbf1;color:#0f766e}
body.light .k-MAPPER{background:#cffafe;color:#0e7490}

/* Tabs */
.tabs{display:flex;gap:0;border-bottom:1px solid var(--border);background:var(--surface);flex-shrink:0}
.tab{padding:8px 18px;font-size:12px;font-weight:600;cursor:pointer;color:var(--text-muted);
  border-bottom:2px solid transparent;transition:all .15s;user-select:none}
.tab:hover{color:var(--text)}
.tab.active{color:var(--text);border-bottom-color:#3b82f6}
.tab-panel{display:none;flex:1;min-height:0;overflow:hidden;flex-direction:column}
.tab-panel.active{display:flex}

/* Graph overlay */
#graph-overlay{display:none;position:fixed;inset:0;background:var(--bg);z-index:100;flex-direction:column}
#graph-overlay.open{display:flex}
#graph-overlay-header{display:flex;align-items:center;gap:12px;padding:10px 18px;
  background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0}
#graph-overlay-header h2{font-size:14px;font-weight:600;color:var(--text)}
#btn-graph-close,#btn-graph-copy{background:transparent;border:1px solid var(--border);
  border-radius:5px;padding:5px 12px;color:var(--text-dim);font-size:12px;cursor:pointer}
#btn-graph-close:hover,#btn-graph-copy:hover{background:var(--border);color:var(--text)}
#btn-graph-close{margin-left:auto}
#graph-area{flex:1;overflow:auto;position:relative;cursor:grab;background:var(--bg)}
#graph-area:active{cursor:grabbing}
#graph-svg-wrap{display:inline-block;padding:40px 48px;transform-origin:0 0}

/* Toolbar */
.toolbar{display:flex;align-items:center;gap:5px;padding:6px 12px;
  border-bottom:1px solid var(--border);background:var(--surface);flex-shrink:0}
.toolbar button{background:transparent;border:1px solid var(--border);border-radius:5px;
  padding:4px 10px;color:var(--text-dim);font-size:11px;cursor:pointer;transition:all .15s}
.toolbar button:hover{background:var(--border);color:var(--text)}
#btn-theme{padding:4px 8px;font-size:13px;background:transparent;border:1px solid var(--border);
  border-radius:5px;cursor:pointer}
#btn-theme:hover{background:var(--border)}

/* Inspector column */
.inspector{width:260px;flex-shrink:0;border-left:1px solid var(--border);
  background:var(--surface);display:flex;flex-direction:row;overflow:hidden;
  transition:width .2s ease}
.inspector.collapsed{width:28px}
/* Collapse toggle strip */
.inspector-toggle{width:28px;flex-shrink:0;display:flex;flex-direction:column;
  align-items:center;padding-top:10px;gap:0;border-right:1px solid var(--border);
  cursor:pointer;user-select:none}
.inspector-toggle:hover{background:var(--border)}
.inspector-toggle-arrow{font-size:11px;color:var(--text-muted);line-height:1;
  transition:transform .2s}
.inspector.collapsed .inspector-toggle-arrow{transform:rotate(180deg)}
/* Inspector inner (hides when collapsed) */
.inspector-inner{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
.inspector.collapsed .inspector-inner{visibility:hidden}
.inspector-header{display:flex;align-items:center;gap:8px;padding:10px 14px;
  border-bottom:1px solid var(--border);flex-shrink:0;min-height:38px}
.inspector-header-title{font-size:12px;font-weight:700;color:var(--text);flex:1;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.inspector-close{background:none;border:none;color:var(--text-muted);font-size:13px;
  cursor:pointer;line-height:1;padding:2px 4px;border-radius:3px;flex-shrink:0;display:none}
.inspector-close:hover{background:var(--border);color:var(--text)}
.inspector.has-detail .inspector-close{display:block}
.inspector-body{flex:1;overflow-y:auto}
.inspector-empty{padding:32px 16px;color:var(--text-muted);font-size:12px;
  text-align:center;line-height:1.7}
.nd-row{display:flex;flex-direction:column;gap:3px;padding:10px 14px;
  border-bottom:1px solid var(--border)}
.nd-row:last-child{border-bottom:none}
.nd-label{font-size:10px;font-weight:700;color:var(--text-muted);text-transform:uppercase;
  letter-spacing:.06em;margin-bottom:2px}
.nd-val{font-size:12px;color:var(--text);word-break:break-word;line-height:1.5}
.nd-val code{font-family:'SF Mono','Fira Code',monospace;font-size:11px;color:var(--text-code)}
.nd-tag{display:inline-block;background:var(--chip-bg);border:1px solid var(--chip-border);
  border-radius:3px;padding:1px 5px;font-size:10px;color:var(--chip-text);margin:2px 2px 0 0}
.nd-tag-link{cursor:pointer}
.nd-tag-link:hover{background:var(--border);color:var(--text)}
/* SVG node highlight when selected */
[data-name].node-selected > rect{stroke:#3b82f6 !important;stroke-width:2px}
/* Narrow viewport: inspector becomes bottom drawer */
@media(max-width:960px){
  .inspector{position:fixed;bottom:0;left:0;right:0;width:auto !important;max-width:none;
    height:0;border-left:none;border-top:1px solid var(--border);
    flex-direction:column;z-index:50;transition:height .25s ease;overflow:hidden}
  .inspector.has-detail{height:360px}
  .inspector-toggle{display:none}
  .inspector.collapsed{width:auto !important}
  .inspector.collapsed .inspector-inner{visibility:visible}
}

/* Non-HTTP entrypoint labels */
.ep-kind-SCHEDULED{font-size:9px;font-weight:700;padding:2px 5px;border-radius:3px;
  background:#37415120;color:#9ca3af;border:1px solid #374151}
.ep-kind-KAFKA{font-size:9px;font-weight:700;padding:2px 5px;border-radius:3px;
  background:#0f766e20;color:#2dd4bf;border:1px solid #0f766e}
.ep-kind-EVENT{font-size:9px;font-weight:700;padding:2px 5px;border-radius:3px;
  background:#4338ca20;color:#a5b4fc;border:1px solid #4338ca}

/* Warnings badge */
#warnings-badge{display:none;align-items:center;gap:6px;padding:5px 10px;
  background:#451a0320;border:1px solid #92400e;border-radius:5px;
  font-size:11px;color:#fbbf24;cursor:pointer}
body.light #warnings-badge{background:#fef3c7;border-color:#d97706;color:#92400e}
#warnings-badge.has-warnings{display:flex}
#warnings-panel{display:none;position:absolute;bottom:40px;right:10px;width:340px;
  background:var(--surface);border:1px solid var(--border);border-radius:8px;
  padding:14px;z-index:30;box-shadow:0 4px 24px #0006;max-height:300px;overflow-y:auto}
#warnings-panel.open{display:block}
#warnings-panel h4{font-size:11px;font-weight:700;color:#fbbf24;margin-bottom:8px;
  text-transform:uppercase;letter-spacing:.06em}
body.light #warnings-panel h4{color:#92400e}
.warning-item{font-size:11px;color:var(--text-dim);padding:4px 0;
  border-bottom:1px solid var(--border)}
.warning-item:last-child{border-bottom:none}

/* Footer with scan metadata */
/* Watch reload banner */
#reload-banner{display:none;align-items:center;gap:10px;padding:7px 18px;
  background:#1e3a5f;border-bottom:1px solid #2563eb;font-size:12px;color:#93c5fd;
  flex-shrink:0}
#reload-banner.visible{display:flex}
#reload-banner strong{color:#dbeafe}
#btn-reload{background:#2563eb;border:none;border-radius:4px;padding:3px 10px;
  color:#fff;font-size:11px;font-weight:600;cursor:pointer}
#btn-reload:hover{background:#1d4ed8}
#btn-reload-dismiss{margin-left:auto;background:none;border:none;color:#93c5fd;
  font-size:14px;cursor:pointer;line-height:1;padding:0}

footer{background:var(--surface);border-top:1px solid var(--border);padding:6px 18px;
  display:flex;align-items:center;gap:16px;flex-shrink:0;font-size:10px;color:var(--text-muted)}
footer span{display:flex;align-items:center;gap:4px}
footer code{font-family:'SF Mono','Fira Code',monospace;color:var(--text-dim)}

/* External systems overlay */
#ext-overlay{display:none;position:fixed;inset:0;background:var(--bg);z-index:100;flex-direction:column}
#ext-overlay.open{display:flex}
#ext-overlay-header{display:flex;align-items:center;gap:12px;padding:10px 18px;
  background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0}
#ext-overlay-header h2{font-size:14px;font-weight:600;color:var(--text)}
#btn-ext-close{margin-left:auto;background:transparent;border:1px solid var(--border);
  border-radius:5px;padding:5px 12px;color:var(--text-dim);font-size:12px;cursor:pointer}
#btn-ext-close:hover{background:var(--border);color:var(--text)}
#ext-body{flex:1;overflow-y:auto;padding:24px 32px;display:grid;
  grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px;align-content:start}
.ext-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px}
.ext-card h3{font-size:13px;font-weight:700;color:var(--text);margin-bottom:8px;
  display:flex;align-items:center;gap:8px}
.ext-card-section{font-size:10px;font-weight:700;color:var(--text-muted);
  text-transform:uppercase;letter-spacing:.06em;margin:8px 0 4px}
.ext-card-item{font-size:12px;color:var(--text-dim);padding:2px 0}

/* Stats overlay */
#stats-overlay{display:none;position:fixed;inset:0;background:var(--bg);z-index:100;flex-direction:column}
#stats-overlay.open{display:flex}
#stats-overlay-header{display:flex;align-items:center;gap:12px;padding:10px 18px;
  background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0}
#stats-overlay-header h2{font-size:14px;font-weight:600;color:var(--text)}
#btn-stats-close{margin-left:auto;background:transparent;border:1px solid var(--border);
  border-radius:5px;padding:5px 12px;color:var(--text-dim);font-size:12px;cursor:pointer}
#btn-stats-close:hover{background:var(--border);color:var(--text)}
#stats-body{flex:1;overflow-y:auto;padding:24px 32px;display:flex;flex-direction:column;gap:24px}
.stats-section{display:flex;flex-direction:column;gap:8px}
.stats-section-title{font-size:11px;font-weight:700;color:var(--text-muted);
  text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px}
.stats-kpi-row{display:flex;gap:12px;flex-wrap:wrap}
.stats-kpi{background:var(--surface);border:1px solid var(--border);border-radius:8px;
  padding:14px 18px;min-width:120px;flex:1}
.stats-kpi-value{font-size:24px;font-weight:700;color:var(--text);font-family:'SF Mono','Fira Code',monospace}
.stats-kpi-label{font-size:11px;color:var(--text-muted);margin-top:2px}
.stats-table{width:100%;border-collapse:collapse;font-size:12px}
.stats-table th{text-align:left;font-size:10px;font-weight:700;color:var(--text-muted);
  text-transform:uppercase;letter-spacing:.06em;padding:6px 10px;
  border-bottom:1px solid var(--border)}
.stats-table td{padding:6px 10px;color:var(--text-dim);border-bottom:1px solid var(--border)}
.stats-table tr:last-child td{border-bottom:none}
.stats-table tr:hover td{background:var(--hover);color:var(--text);cursor:pointer}
.stats-bar-wrap{display:flex;align-items:center;gap:8px}
.stats-bar-track{flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden}
.stats-bar-fill{height:100%;border-radius:3px;background:#3b82f6;transition:width .3s}
.stats-tag{display:inline-block;font-size:9px;font-weight:700;padding:1px 5px;
  border-radius:3px;margin-left:4px;vertical-align:middle}
.stats-warn{color:#fbbf24}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}

::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--scrollbar);border-radius:3px}

</style>
</head>
<body>
<div id="reload-banner">
  <strong>Architecture updated</strong>
  <span id="reload-banner-detail"></span>
  <button id="btn-reload" onclick="location.reload()">Reload</button>
  <button id="btn-reload-dismiss">✕</button>
</div>
<header>
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
       stroke="#3b82f6" stroke-width="2" stroke-linecap="round">
    <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
  </svg>
  <h1>{{TITLE}}</h1>
  <span class="stats">{{STATS}}</span>
  <button id="btn-theme" title="Toggle light/dark mode">☀️</button>
</header>

<div class="layout">
  <div class="sidebar">
    <div class="sb-tabs" id="sb-tabs">
      <div class="sb-tab active" data-sb="HTTP">HTTP</div>
      <div class="sb-tab" data-sb="JOBS">Jobs</div>
      <div class="sb-tab" data-sb="EVENTS">Events</div>
      <div class="sb-tab" data-sb="SEARCH">Search</div>
    </div>
    <div class="sb-search-wrap" id="sb-search-wrap">
      <input id="q" type="text" placeholder="Components, endpoints, packages…" autocomplete="off"/>
    </div>
    <div class="sidebar-body" id="sb"></div>
    <div class="legend" id="legend"></div>
  </div>

  <div class="main">
    <div class="toolbar">
      <button id="btn-reset">Reset</button>
      <button id="btn-copy" title="Copy chain as image">Copy</button>
      <button id="btn-graph-open">Graph</button>
      <button id="btn-ext-open">Externals</button>
      <button id="btn-stats-open">Stats</button>
    </div>
    <div class="tabs">
      <div class="tab active" data-tab="chain">Endpoints</div>
    </div>

    <div class="tab-panel active" id="tab-chain">
      <div id="chain-area">
        <div id="chain-empty">
          <div id="chain-empty-inner">
            <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><circle cx="18" cy="18" r="3"/><path d="M9 6h6M18 9v6"/></svg>
            <p>Select an endpoint to explore its call chain</p>
          </div>
        </div>
        <div id="chain-svg"></div>
      </div>
      <div class="detail hidden" id="detail"></div>
    </div>

  </div>

  <div class="inspector" id="inspector">
    <div class="inspector-toggle" id="inspector-toggle" title="Toggle inspector">
      <span class="inspector-toggle-arrow">&#x276E;</span>
    </div>
    <div class="inspector-inner">
      <div class="inspector-header">
        <span class="inspector-header-title" id="nd-title">Inspector</span>
        <button class="inspector-close" id="node-detail-close" title="Clear selection">✕</button>
      </div>
      <div class="inspector-body" id="nd-body">
        <div class="inspector-empty">Click any node in the map to inspect it.</div>
      </div>
    </div>
  </div>
</div>

<div id="graph-overlay">
  <div id="graph-overlay-header">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2"><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><circle cx="18" cy="18" r="3"/><circle cx="6" cy="18" r="3"/><path d="M9 6h6M18 9v6M9 18h6M6 9v6"/></svg>
    <h2>Full component graph</h2>
    <button id="btn-graph-copy">Copy image</button>
    <button id="btn-graph-close">✕ Close</button>
  </div>
  <div id="graph-area">
    <div id="graph-svg-wrap"></div>
  </div>
</div>

<div id="ext-overlay">
  <div id="ext-overlay-header">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#64748b" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 010 20M12 2a15 15 0 000 20"/></svg>
    <h2>External systems</h2>
    <button id="btn-ext-close">✕ Close</button>
  </div>
  <div id="ext-body"></div>
</div>

<div id="stats-overlay">
  <div id="stats-overlay-header">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2"><path d="M18 20V10M12 20V4M6 20v-6"/></svg>
    <h2>Codebase statistics</h2>
    <button id="btn-stats-close">✕ Close</button>
  </div>
  <div id="stats-body"></div>
</div>

<div id="warnings-panel">
  <h4>⚠ Scan warnings</h4>
  <div id="warnings-list"></div>
</div>

<footer>
  <span id="meta-root"></span>
  <span id="meta-git"></span>
  <span id="meta-time"></span>
  <span id="meta-version"></span>
  <span id="warnings-badge" title="Click to see scan warnings">⚠ <span id="warnings-count"></span> warnings</span>
</footer>

<script>
const NODES_RAW   = {{GRAPH_NODES}};
const EDGES_RAW   = {{GRAPH_EDGES}};
const SIDEBAR_RAW = {{SIDEBAR_DATA}};
const COMP        = {{COMP_DATA}};
const SCAN_META   = {{SCAN_META}};
const WARNINGS    = {{WARNINGS}};

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── Chain SVG renderer ────────────────────────────────────────────────────────
const KIND_COLOR = {
  CONTROLLER: {bg:'#1d4ed8', border:'#60a5fa', text:'#fff'},
  SERVICE:    {bg:'#15803d', border:'#4ade80', text:'#fff'},
  REPOSITORY: {bg:'#7e22ce', border:'#c084fc', text:'#fff'},
  CLIENT:     {bg:'#b45309', border:'#fbbf24', text:'#fff'},
  GATEWAY:    {bg:'#be123c', border:'#fb7185', text:'#fff'},
  CONSUMER:   {bg:'#0f766e', border:'#2dd4bf', text:'#fff'},
  MAPPER:     {bg:'#0e7490', border:'#22d3ee', text:'#fff'},
  VALIDATOR:  {bg:'#a16207', border:'#facc15', text:'#fff'},
  FACADE:     {bg:'#1d4ed8', border:'#93c5fd', text:'#fff'},
  LISTENER:   {bg:'#4338ca', border:'#a5b4fc', text:'#fff'},
  CACHE:      {bg:'#065f46', border:'#34d399', text:'#fff'},
  CONFIG:     {bg:'#292524', border:'#78716c', text:'#d6d3d1'},
  COMPONENT:  {bg:'#334155', border:'#64748b', text:'#e2e8f0'},
  EXTERNAL:   {bg:'#1e293b', border:'#64748b', text:'#94a3b8'},
};

function renderChainSVG(ctrlName, ep) {
  const NW = 220, NH = 40, HGAP = 100, VGAP = 62, PAD_X = 48, PAD_Y = 72;

  // BFS from controller through dependencies, building columns by distance
  const colOf = new Map(); // name → column index
  const queue = [...(ep.calls || []).filter(s => COMP[s])];
  queue.forEach(s => colOf.set(s, 1));
  colOf.set(ctrlName, 0);

  let head = 0;
  while (head < queue.length) {
    const name = queue[head++];
    const col = colOf.get(name);
    (COMP[name]?.dependencies || []).forEach(dep => {
      if (COMP[dep] && !colOf.has(dep)) {
        colOf.set(dep, col + 1);
        queue.push(dep);
      }
    });
  }

  // External systems always go in the last column
  const extSet = new Set();
  for (const name of colOf.keys()) {
    (COMP[name]?.externalSystems || []).forEach(e => extSet.add(e));
  }
  const maxCompCol = colOf.size ? Math.max(...colOf.values()) : 0;
  const extCol = maxCompCol + 1;

  // Build column arrays
  const numCols = extSet.size ? extCol + 1 : maxCompCol + 1;
  const cols = Array.from({length: numCols}, () => []);
  for (const [name, col] of colOf) cols[col].push(name);
  for (const ext of extSet) cols[extCol] = cols[extCol] || [];
  if (extSet.size) cols[extCol].push(...extSet);

  const nonEmpty = cols.filter(c => c.length > 0);
  if (nonEmpty.length === 0) return '<p style="padding:40px;color:#6b7280">No call chain data detected.</p>';

  const maxRows = Math.max(...nonEmpty.map(c => c.length));
  const svgW = PAD_X * 2 + nonEmpty.length * NW + (nonEmpty.length - 1) * HGAP;
  const svgH = PAD_Y * 2 + maxRows * VGAP;

  // Assign pixel positions
  const pos = {};
  nonEmpty.forEach((col, ci) => {
    const x = PAD_X + ci * (NW + HGAP);
    const offsetY = ((maxRows - col.length) / 2) * VGAP;
    col.forEach((name, ri) => { pos[name] = { x, y: PAD_Y + offsetY + ri * VGAP }; });
  });

  function nodeColor(name) {
    if (!COMP[name]) return KIND_COLOR.EXTERNAL;
    return KIND_COLOR[COMP[name].kind] || KIND_COLOR.COMPONENT;
  }
  function escXml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
  function truncate(s, max=32) { return s.length > max ? s.slice(0, max-1) + '\u2026' : s; }
  function nameFontSize(s) { return s.length > 22 ? 11 : s.length > 17 ? 12 : 13; }

  function edge(fromName, toName, dashed=false) {
    const p1 = pos[fromName], p2 = pos[toName];
    if (!p1 || !p2) return '';
    const dash = dashed ? ' stroke-dasharray="6 3"' : '';
    // Forward edge (normal left-to-right)
    if (p2.x > p1.x + NW/2) {
      const x1 = p1.x + NW, y1 = p1.y + NH/2;
      const x2 = p2.x,       y2 = p2.y + NH/2;
      const cx = (x1 + x2) / 2;
      return `<path d="M${x1} ${y1} C${cx} ${y1} ${cx} ${y2} ${x2} ${y2}" fill="none" stroke="#475569" stroke-width="1.5"${dash} marker-end="url(#arr)"/>`;
    }
    // Back-edge or same-column: arc below the nodes
    const x1 = p1.x + NW/2, y1 = p1.y + NH;
    const x2 = p2.x + NW/2, y2 = p2.y + NH;
    const cy = Math.max(y1, y2) + 36;
    return `<path d="M${x1} ${y1} C${x1} ${cy} ${x2} ${cy} ${x2} ${y2}" fill="none" stroke="#64748b" stroke-width="1" stroke-dasharray="4 3" opacity="0.6" marker-end="url(#arr)"/>`;
  }

  function node(name) {
    const p = pos[name]; if (!p) return '';
    const c = nodeColor(name);
    const kind = COMP[name]?.kind || 'EXTERNAL';
    const label = escXml(truncate(name));
    const kindLabel = escXml(kind.charAt(0) + kind.slice(1).toLowerCase());
    const fs = nameFontSize(name);
    return `<g class="chain-node" data-name="${escXml(name)}" style="cursor:pointer">
      <rect x="${p.x}" y="${p.y}" width="${NW}" height="${NH}" rx="6"
        fill="${c.bg}" stroke="${c.border}" stroke-width="1.5"/>
      <text x="${p.x + NW/2}" y="${p.y + 14}" text-anchor="middle"
        fill="${c.text}" font-size="10" font-family="system-ui" opacity="0.7">${kindLabel}</text>
      <text x="${p.x + NW/2}" y="${p.y + 28}" text-anchor="middle"
        fill="${c.text}" font-size="${fs}" font-family="system-ui" font-weight="600">${label}</text>
    </g>`;
  }

  // Edges: BFS tree + external systems
  let edges = '';
  for (const [name] of colOf) {
    (COMP[name]?.dependencies || []).forEach(dep => { if (pos[dep]) edges += edge(name, dep); });
    (COMP[name]?.externalSystems || []).forEach(e => { if (pos[e]) edges += edge(name, e, true); });
  }

  let nodes = '';
  nonEmpty.flat().forEach(name => { nodes += node(name); });

  const methodColor = {GET:'#4ade80',POST:'#60a5fa',PUT:'#fbbf24',DELETE:'#f87171',PATCH:'#c084fc',ANY:'#64748b'};
  const mc = methodColor[ep.method] || '#64748b';
  const title = `
    <rect x="0" y="0" width="${svgW}" height="44" fill="none"/>
    <rect x="${PAD_X}" y="14" width="auto" height="24" rx="4" fill="${mc}22" stroke="${mc}" stroke-width="1"/>
    <text x="${PAD_X + 8}" y="30" font-family="system-ui" font-size="11" font-weight="800" fill="${mc}">${ep.method}</text>
    <text x="${PAD_X + 8 + (ep.method.length * 7) + 6}" y="30" font-family="'SF Mono','Fira Code',monospace" font-size="12" fill="#94a3b8">${escXml(ep.path)}</text>
  `;

  return `<svg xmlns="http://www.w3.org/2000/svg" width="${svgW}" height="${svgH}">
    <defs>
      <marker id="arr" viewBox="0 0 10 10" refX="9" refY="5"
        markerWidth="6" markerHeight="6" orient="auto">
        <path d="M0,0 L10,5 L0,10 z" fill="#475569"/>
      </marker>
    </defs>
    ${title}${edges}${nodes}
  </svg>`;
}
// ── Sidebar ───────────────────────────────────────────────────────────────────
const KINDS = [
  ['CONTROLLER','#3b82f6'],['SERVICE','#22c55e'],['REPOSITORY','#a855f7'],
  ['CLIENT','#f59e0b'],['GATEWAY','#f43f5e'],['CONSUMER','#14b8a6'],
  ['MAPPER','#22d3ee'],['CACHE','#34d399'],['CONFIG','#78716c'],['EXTERNAL','#64748b'],
];
const legendEl = document.getElementById('legend');
KINDS.forEach(([k,c]) => {
  legendEl.innerHTML += `<span class="legend-item">
    <span class="legend-dot" style="background:${c}"></span>
    <span>${k.charAt(0)+k.slice(1).toLowerCase()}</span>
  </span>`;
});

// ── Sidebar tab state ─────────────────────────────────────────────────────────
let sbTab = 'HTTP';

function switchSbTab(tab) {
  sbTab = tab;
  document.querySelectorAll('.sb-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.sb === tab));
  const searchWrap = document.getElementById('sb-search-wrap');
  searchWrap.classList.toggle('visible', tab === 'SEARCH');
  if (tab === 'SEARCH') {
    document.getElementById('q').focus();
    renderSearch('');
  } else {
    renderSidebar();
  }
}

document.querySelectorAll('.sb-tab').forEach(t =>
  t.addEventListener('click', () => switchSbTab(t.dataset.sb)));
document.getElementById('q').addEventListener('input', e => renderSearch(e.target.value));

// ── HTTP tab ──────────────────────────────────────────────────────────────────
function renderSidebar() {
  const sb = document.getElementById('sb');
  sb.innerHTML = '';
  if (sbTab === 'JOBS') { renderJobsTab(sb); return; }
  if (sbTab === 'EVENTS') { renderEventsTab(sb); return; }
  if (sbTab === 'SEARCH') return; // handled by renderSearch

  // HTTP
  SIDEBAR_RAW.forEach(ctrl => {
    const eps = ctrl.endpoints || [];
    if (!eps.length) return;
    const grp = document.createElement('div');
    grp.className = 'ctrl-group';
    grp.innerHTML = `<div class="ctrl-header">
      ${escHtml(ctrl.controller)}
      ${ctrl.domain ? `<span class="domain-chip">${escHtml(ctrl.domain)}</span>` : ''}
    </div><div class="ep-list"></div>`;
    sb.appendChild(grp);
    const list = grp.querySelector('.ep-list');
    eps.forEach(ep => {
      const item = document.createElement('div');
      item.className = 'ep-item';
      item.innerHTML = `<span class="method m-${escHtml(ep.method)}" style="flex-shrink:0">${escHtml(ep.method)}</span>
        <div style="display:flex;flex-direction:column;gap:1px;min-width:0;overflow:hidden">
          <span class="ep-path">${escHtml(ep.path)}</span>
          <span style="font-size:10px;color:var(--text-muted);font-family:'SF Mono','Fira Code',monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escHtml(ep.handler)}()</span>
        </div>`;
      item.addEventListener('click', () => selectEndpoint(ctrl.controller, ep, item));
      list.appendChild(item);
    });
  });
}

// ── Jobs tab (@Scheduled) ─────────────────────────────────────────────────────
function renderJobsTab(sb) {
  let any = false;
  SIDEBAR_RAW.forEach(ctrl => {
    const jobs = (ctrl.nonHttpEntrypoints || []).filter(e => e.kind === 'SCHEDULED');
    if (!jobs.length) return;
    any = true;
    const grp = document.createElement('div');
    grp.className = 'ctrl-group';
    grp.innerHTML = `<div class="ctrl-header">${escHtml(ctrl.controller)}
      ${ctrl.domain ? `<span class="domain-chip">${escHtml(ctrl.domain)}</span>` : ''}
    </div><div class="ep-list"></div>`;
    sb.appendChild(grp);
    jobs.forEach(e => {
      const item = document.createElement('div');
      item.className = 'ep-item';
      item.innerHTML = `<span class="ep-kind-SCHEDULED" style="flex-shrink:0">CRON</span>
        <div style="display:flex;flex-direction:column;gap:1px;min-width:0;overflow:hidden">
          <span class="ep-path" style="font-family:'SF Mono','Fira Code',monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escHtml(e.method)}()</span>
          ${e.detail ? `<span style="font-size:10px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escHtml(e.detail)}</span>` : ''}
        </div>`;
      item.addEventListener('click', () => {
        document.querySelectorAll('.ep-item').forEach(el => el.classList.remove('active'));
        item.classList.add('active');
        showNodeDetail(ctrl.controller);
      });
      grp.querySelector('.ep-list').appendChild(item);
    });
  });
  if (!any) sb.innerHTML = '<div style="padding:24px 16px;color:var(--text-muted);font-size:12px">No @Scheduled methods detected.</div>';
}

// ── Events tab (@EventListener / @KafkaListener) ──────────────────────────────
function renderEventsTab(sb) {
  let any = false;
  SIDEBAR_RAW.forEach(ctrl => {
    const evts = (ctrl.nonHttpEntrypoints || []).filter(e => e.kind === 'EVENT' || e.kind === 'KAFKA');
    if (!evts.length) return;
    any = true;
    const grp = document.createElement('div');
    grp.className = 'ctrl-group';
    grp.innerHTML = `<div class="ctrl-header">${escHtml(ctrl.controller)}
      ${ctrl.domain ? `<span class="domain-chip">${escHtml(ctrl.domain)}</span>` : ''}
    </div><div class="ep-list"></div>`;
    sb.appendChild(grp);
    evts.forEach(e => {
      const item = document.createElement('div');
      item.className = 'ep-item';
      const badge = e.kind === 'KAFKA' ? 'KAFKA' : 'EVENT';
      item.innerHTML = `<span class="ep-kind-${escHtml(badge)}" style="flex-shrink:0">${escHtml(badge)}</span>
        <div style="display:flex;flex-direction:column;gap:1px;min-width:0;overflow:hidden">
          <span class="ep-path" style="font-family:'SF Mono','Fira Code',monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escHtml(e.method)}()</span>
          ${e.detail ? `<span style="font-size:10px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escHtml(e.detail)}</span>` : ''}
        </div>`;
      item.addEventListener('click', () => {
        document.querySelectorAll('.ep-item').forEach(el => el.classList.remove('active'));
        item.classList.add('active');
        showNodeDetail(ctrl.controller);
      });
      grp.querySelector('.ep-list').appendChild(item);
    });
  });
  if (!any) sb.innerHTML = '<div style="padding:24px 16px;color:var(--text-muted);font-size:12px">No @EventListener or @KafkaListener methods detected.</div>';
}

// ── Search tab ────────────────────────────────────────────────────────────────
function renderSearch(q) {
  const sb = document.getElementById('sb');
  sb.innerHTML = '';
  const ql = q.trim().toLowerCase();
  if (!ql) {
    sb.innerHTML = '<div style="padding:24px 16px;color:var(--text-muted);font-size:12px">Type to search components, endpoints, packages, or external systems.</div>';
    return;
  }

  const results = [];

  // Endpoints
  SIDEBAR_RAW.forEach(ctrl => {
    (ctrl.endpoints || []).forEach(ep => {
      if (ep.path.toLowerCase().includes(ql) || ep.handler.toLowerCase().includes(ql)) {
        results.push({kind:'Endpoint', name:`${ep.method} ${ep.path}`, sub:ctrl.controller, action:()=>selectEndpoint(ctrl.controller,ep,null)});
      }
    });
  });

  // Components
  Object.values(COMP).forEach(c => {
    if (c.name.toLowerCase().includes(ql) || c.package.toLowerCase().includes(ql)) {
      results.push({kind:c.kind.charAt(0)+c.kind.slice(1).toLowerCase(), name:c.name, sub:c.package, action:()=>showNodeDetail(c.name)});
    }
  });

  // External systems
  const extSeen = new Set();
  Object.values(COMP).forEach(c => {
    (c.externalSystems||[]).forEach(ext => {
      if (!extSeen.has(ext) && ext.toLowerCase().includes(ql)) {
        extSeen.add(ext);
        const callers = Object.values(COMP).filter(x=>x.externalSystems?.includes(ext)).map(x=>x.name);
        results.push({kind:'External', name:ext, sub:`Called by: ${callers.slice(0,3).join(', ')}`, action:()=>{}});
      }
    });
  });

  if (!results.length) {
    sb.innerHTML = `<div style="padding:24px 16px;color:var(--text-muted);font-size:12px">No results for "${escHtml(q)}"</div>`;
    return;
  }

  results.slice(0, 40).forEach(r => {
    const div = document.createElement('div');
    div.className = 'search-result';
    div.innerHTML = `<div class="search-result-kind">${escHtml(r.kind)}</div>
      <div class="search-result-name">${escHtml(r.name)}</div>
      <div class="search-result-sub">${escHtml(r.sub)}</div>`;
    div.addEventListener('click', r.action);
    sb.appendChild(div);
  });
}

// ── Endpoint selection ────────────────────────────────────────────────────────
function selectEndpoint(ctrlName, ep, itemEl){
  document.querySelectorAll('.ep-item').forEach(el=>el.classList.remove('active'));
  if(itemEl) itemEl.classList.add('active');

  // Render SVG chain
  const chainSvg = document.getElementById('chain-svg');
  chainSvg.innerHTML = renderChainSVG(ctrlName, ep);
  document.getElementById('chain-empty').classList.add('hidden');

  // Show endpoint header in detail panel
  const panel = document.getElementById('detail');
  panel.classList.remove('hidden');
  const noCallsNote = !ep.calls.length
    ? '<span style="opacity:.5;font-size:11px">— no service calls detected in handler body</span>' : '';
  panel.innerHTML=`<div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap">
    <span class="method m-${escHtml(ep.method)}" style="font-size:10px">${escHtml(ep.method)}</span>
    <code style="font-size:13px;color:var(--text)">${escHtml(ep.path)}</code>
    ${noCallsNote}
  </div>`;
}

function resetFilter(){
  document.getElementById('chain-svg').innerHTML = '';
  document.getElementById('chain-empty').classList.remove('hidden');
  document.querySelectorAll('.ep-item').forEach(el=>el.classList.remove('active'));
  document.getElementById('detail').classList.add('hidden');
}

// ── Controls ──────────────────────────────────────────────────────────────────
document.getElementById('btn-reset').addEventListener('click', resetFilter);

function copySvgToPng(svg, btn, filename) {
  if (!svg) return;
  const isDark = !document.body.classList.contains('light');
  const bg = isDark ? '#0b1120' : '#f1f5f9';
  const w = svg.getAttribute('width'), h = svg.getAttribute('height');
  const clone = svg.cloneNode(true);
  const rect = document.createElementNS('http://www.w3.org/2000/svg','rect');
  rect.setAttribute('width', w); rect.setAttribute('height', h); rect.setAttribute('fill', bg);
  clone.insertBefore(rect, clone.firstChild);
  const blob = new Blob([clone.outerHTML], {type:'image/svg+xml'});
  const url = URL.createObjectURL(blob);
  const img = new Image();
  img.onload = () => {
    const canvas = document.createElement('canvas');
    const scale = 2;
    canvas.width = w * scale; canvas.height = h * scale;
    const ctx = canvas.getContext('2d');
    ctx.scale(scale, scale);
    ctx.drawImage(img, 0, 0);
    URL.revokeObjectURL(url);
    const origText = btn.textContent;
    canvas.toBlob(pngBlob => {
      navigator.clipboard.write([new ClipboardItem({'image/png': pngBlob})])
        .then(() => { btn.textContent = 'Copied!'; setTimeout(() => btn.textContent = origText, 1800); })
        .catch(() => {
          const a = document.createElement('a');
          a.href = canvas.toDataURL('image/png');
          a.download = filename;
          a.click();
        });
    });
  };
  img.src = url;
}

document.getElementById('btn-copy').addEventListener('click', () => {
  copySvgToPng(document.querySelector('#chain-svg svg'), document.getElementById('btn-copy'), 'chain.png');
});
document.getElementById('btn-graph-copy').addEventListener('click', () => {
  copySvgToPng(document.querySelector('#graph-svg-wrap svg'), document.getElementById('btn-graph-copy'), 'graph.png');
});
// ── Theme toggle ──────────────────────────────────────────────────────────────
(function(){
  const btn = document.getElementById('btn-theme');
  if(localStorage.getItem('codemap-theme')==='light') document.body.classList.add('light');
  function applyTheme(){
    const light = document.body.classList.contains('light');
    btn.textContent = light ? '🌙' : '☀️';
  }
  btn.addEventListener('click', ()=>{
    document.body.classList.toggle('light');
    localStorage.setItem('codemap-theme', document.body.classList.contains('light')?'light':'dark');
    applyTheme();
  });
  applyTheme();
})();

// ── Full component graph ──────────────────────────────────────────────────────
function renderFullGraph() {
  const NW = 210, NH = 38, HGAP = 120, VGAP = 56, PAD_X = 60, PAD_Y = 40;
  const COLS = {CONTROLLER:0,SERVICE:1,FACADE:1,CONSUMER:1,LISTENER:1,SCHEDULER:1,
                CLIENT:2,GATEWAY:2,MAPPER:2,VALIDATOR:2,COMPONENT:2,
                REPOSITORY:3,EXTERNAL:4};

  // Group nodes by column
  const cols = [[],[],[],[],[]];
  const extNames = new Set();
  Object.values(COMP).forEach(c => {
    c.externalSystems.forEach(e => extNames.add(e));
  });
  Object.values(COMP).forEach(c => {
    const col = COLS[c.kind] ?? 2;
    cols[col].push(c.name);
  });
  extNames.forEach(e => cols[4].push(e));
  cols.forEach(col => col.sort());

  const nonEmpty = cols.filter(c=>c.length>0);
  const maxRows = Math.max(...nonEmpty.map(c=>c.length));
  const svgW = PAD_X*2 + nonEmpty.length*NW + (nonEmpty.length-1)*HGAP;
  const svgH = PAD_Y*2 + maxRows*VGAP;

  // Position map
  const pos = {};
  let ci = 0;
  cols.forEach((col) => {
    if (!col.length) return;
    const x = PAD_X + ci*(NW+HGAP);
    const offsetY = ((maxRows - col.length)/2)*VGAP;
    col.forEach((name,ri) => { pos[name] = {x, y: PAD_Y + offsetY + ri*VGAP}; });
    ci++;
  });

  function escXml(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function truncate(s,max=30){ return s.length>max ? s.slice(0,max-1)+'…':s; }
  function nameFontSize(s){ return s.length>22 ? 9 : s.length>17 ? 10 : 11; }

  function nodeColor(name) {
    if (!COMP[name]) return KIND_COLOR.EXTERNAL;
    return KIND_COLOR[COMP[name].kind] || KIND_COLOR.COMPONENT;
  }

  let edges = '';
  // Draw edges for all components
  Object.values(COMP).forEach(c => {
    c.dependencies.forEach(dep => {
      const p1=pos[c.name], p2=pos[dep]; if(!p1||!p2) return;
      const x1=p1.x+NW, y1=p1.y+NH/2, x2=p2.x, y2=p2.y+NH/2;
      const cx=(x1+x2)/2;
      edges += `<path d="M${x1} ${y1} C${cx} ${y1} ${cx} ${y2} ${x2} ${y2}" fill="none" stroke="#334155" stroke-width="1" marker-end="url(#garr)"/>`;
    });
    c.externalSystems.forEach(e => {
      const p1=pos[c.name], p2=pos[e]; if(!p1||!p2) return;
      const x1=p1.x+NW, y1=p1.y+NH/2, x2=p2.x, y2=p2.y+NH/2;
      const cx=(x1+x2)/2;
      edges += `<path d="M${x1} ${y1} C${cx} ${y1} ${cx} ${y2} ${x2} ${y2}" fill="none" stroke="#1e293b" stroke-width="1" stroke-dasharray="5 3" marker-end="url(#garr)"/>`;
    });
  });

  let nodes = '';
  [...Object.keys(COMP), ...extNames].forEach(name => {
    const p=pos[name]; if(!p) return;
    const c=nodeColor(name);
    const kind=(COMP[name]?.kind||'EXTERNAL');
    const kindLabel=kind.charAt(0)+kind.slice(1).toLowerCase();
    nodes += `<g>
      <rect x="${p.x}" y="${p.y}" width="${NW}" height="${NH}" rx="5" fill="${c.bg}" stroke="${c.border}" stroke-width="1.5"/>
      <text x="${p.x+NW/2}" y="${p.y+12}" text-anchor="middle" fill="${c.text}" font-size="9" font-family="system-ui" opacity="0.65">${escXml(kindLabel)}</text>
      <text x="${p.x+NW/2}" y="${p.y+26}" text-anchor="middle" fill="${c.text}" font-size="${nameFontSize(name)}" font-family="system-ui" font-weight="600">${escXml(truncate(name))}</text>
    </g>`;
  });

  document.getElementById('graph-svg-wrap').innerHTML =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${svgW}" height="${svgH}">
      <defs><marker id="garr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto">
        <path d="M0,0 L10,5 L0,10 z" fill="#475569"/>
      </marker></defs>
      ${edges}${nodes}
    </svg>`;
}

// ── Graph overlay ─────────────────────────────────────────────────────────────
let graphRendered = false;
function openGraph() {
  document.getElementById('graph-overlay').classList.add('open');
  if (!graphRendered) { renderFullGraph(); graphRendered = true; }
}
function closeGraph() {
  document.getElementById('graph-overlay').classList.remove('open');
}
document.getElementById('btn-graph-open').addEventListener('click', openGraph);
document.getElementById('btn-graph-close').addEventListener('click', closeGraph);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeGraph(); });

// ── Tab switching ─────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-'+tab.dataset.tab).classList.add('active');
  });
});

// ── Node detail panel ─────────────────────────────────────────────────────────
let _selectedNode = null;

function _highlightNode(name) {
  document.querySelectorAll('#chain-svg [data-name]').forEach(el => el.classList.remove('node-selected'));
  if (name) document.querySelectorAll(`#chain-svg [data-name="${CSS.escape(name)}"]`).forEach(el => el.classList.add('node-selected'));
  _selectedNode = name;
}

function showNodeDetail(name) {
  const comp = COMP[name];
  const inspector = document.getElementById('inspector');
  const title = document.getElementById('nd-title');
  const body  = document.getElementById('nd-body');
  _highlightNode(name);

  title.textContent = name;
  let html = '';

  if (comp) {
    // Classification
    html += `<div class="nd-row">
      <div class="nd-label">Classification</div>
      <div class="nd-val">${escHtml(comp.kind.charAt(0)+comp.kind.slice(1).toLowerCase())}
        <span style="color:var(--text-muted);font-size:11px"> — ${escHtml(comp.classificationReason)}</span>
      </div>
    </div>`;

    // Annotations
    if (comp.springAnnotations?.length) {
      html += `<div class="nd-row"><div class="nd-label">Spring annotations</div><div class="nd-val">`;
      comp.springAnnotations.forEach(a => { html += `<span class="nd-tag">@${escHtml(a)}</span>`; });
      html += `</div></div>`;
    }

    // File
    if (comp.file) {
      const shortFile = comp.file.replace(/.*\/src\/main\/(kotlin|java)\//, '');
      html += `<div class="nd-row"><div class="nd-label">Source file</div>
        <div class="nd-val"><code>${escHtml(shortFile)}</code></div></div>`;
    }

    // Package / domain
    if (comp.domain || comp.package) {
      html += `<div class="nd-row"><div class="nd-label">Package · Domain</div>
        <div class="nd-val"><code>${escHtml(comp.package)}</code>`;
      if (comp.domain) html += ` <span class="nd-tag">${escHtml(comp.domain)}</span>`;
      html += `</div></div>`;
    }

    // Dependencies
    if (comp.dependencies?.length) {
      html += `<div class="nd-row"><div class="nd-label">Depends on</div><div class="nd-val">`;
      comp.dependencies.forEach(d => { html += `<span class="nd-tag nd-tag-link" data-nav="${escHtml(d)}">${escHtml(d)}</span> `; });
      html += `</div></div>`;
    }

    // External systems
    if (comp.externalSystems?.length) {
      html += `<div class="nd-row"><div class="nd-label">External systems</div><div class="nd-val">`;
      comp.externalSystems.forEach(e => { html += `<span class="nd-tag nd-tag-link" data-nav="${escHtml(e)}" style="color:#94a3b8">${escHtml(e)}</span> `; });
      html += `</div></div>`;
    }

    // HTTP endpoints
    if (comp.endpoints?.length) {
      html += `<div class="nd-row"><div class="nd-label">HTTP endpoints (${comp.endpoints.length})</div><div class="nd-val">`;
      comp.endpoints.forEach(ep => {
        html += `<div style="font-size:11px;padding:2px 0"><span class="method m-${escHtml(ep.method)}" style="font-size:9px">${escHtml(ep.method)}</span> <code>${escHtml(ep.path)}</code></div>`;
      });
      html += `</div></div>`;
    }

    // Non-HTTP entrypoints
    if (comp.nonHttpEntrypoints?.length) {
      html += `<div class="nd-row"><div class="nd-label">Other entrypoints</div><div class="nd-val">`;
      comp.nonHttpEntrypoints.forEach(e => {
        html += `<div style="font-size:11px;padding:2px 0"><span class="ep-kind-${escHtml(e.kind)}">${escHtml(e.kind)}</span> <code>${escHtml(e.method)}()</code>`;
        if (e.detail) html += ` <span style="color:var(--text-muted)">${escHtml(e.detail)}</span>`;
        html += `</div>`;
      });
      html += `</div></div>`;
    }

    // Who depends on this
    const callers = Object.values(COMP).filter(c => c.dependencies?.includes(name)).map(c=>c.name);
    if (callers.length) {
      html += `<div class="nd-row"><div class="nd-label">Called by</div><div class="nd-val">`;
      callers.forEach(c => { html += `<span class="nd-tag nd-tag-link" data-nav="${escHtml(c)}">${escHtml(c)}</span> `; });
      html += `</div></div>`;
    }
  } else {
    // External system node
    const callers = Object.values(COMP).filter(c => c.externalSystems?.includes(name)).map(c=>c.name);
    html += `<div class="nd-row"><div class="nd-label">Type</div><div class="nd-val">External system</div></div>`;
    if (callers.length) {
      html += `<div class="nd-row"><div class="nd-label">Called by</div><div class="nd-val">`;
      callers.forEach(c => { html += `<span class="nd-tag nd-tag-link" data-nav="${escHtml(c)}">${escHtml(c)}</span> `; });
      html += `</div></div>`;
    }
  }

  body.innerHTML = html;
  inspector.classList.add('has-detail');
  inspector.classList.remove('collapsed');
  localStorage.setItem('inspector-collapsed', '0');
}

// Inspector collapse toggle
(function(){
  const inspector = document.getElementById('inspector');
  const toggle = document.getElementById('inspector-toggle');
  if (localStorage.getItem('inspector-collapsed') !== '0') inspector.classList.add('collapsed');
  toggle.addEventListener('click', () => {
    inspector.classList.toggle('collapsed');
    localStorage.setItem('inspector-collapsed', inspector.classList.contains('collapsed') ? '1' : '0');
  });
})();

document.getElementById('node-detail-close').addEventListener('click', () => {
  const inspector = document.getElementById('inspector');
  inspector.classList.remove('has-detail');
  inspector.classList.add('collapsed');
  localStorage.setItem('inspector-collapsed', '1');
  document.getElementById('nd-title').textContent = 'Inspector';
  document.getElementById('nd-body').innerHTML = '<div class="inspector-empty">Click any node in the map to inspect it.</div>';
  _highlightNode(null);
});

// Inspector tag navigation
document.getElementById('nd-body').addEventListener('click', e => {
  const tag = e.target.closest('[data-nav]');
  if (tag) showNodeDetail(tag.dataset.nav);
});

// Wire chain-node clicks (delegated on chain-area so it covers the full scroll surface)
document.getElementById('chain-area').addEventListener('click', e => {
  const node = e.target.closest('[data-name]');
  if (!node || !node.dataset.name) return;
  if (node.dataset.name === _selectedNode) {
    document.getElementById('node-detail-close').click();
  } else {
    showNodeDetail(node.dataset.name);
  }
});

// ── Scan metadata footer ──────────────────────────────────────────────────────
(function() {
  if (SCAN_META.root) {
    const short = SCAN_META.root.replace(/.*\/([^/]+\/[^/]+)$/, '…/$1');
    document.getElementById('meta-root').innerHTML = `📁 <code>${escHtml(short)}</code>`;
  }
  if (SCAN_META.branch || SCAN_META.commit) {
    document.getElementById('meta-git').innerHTML =
      `🌿 <code>${escHtml(SCAN_META.branch)}${SCAN_META.commit ? ' @'+SCAN_META.commit : ''}</code>`;
  }
  if (SCAN_META.timestamp) {
    document.getElementById('meta-time').textContent = '🕐 ' + SCAN_META.timestamp.replace('T',' ');
  }
  document.getElementById('meta-version').textContent = `codemap v${SCAN_META.scannerVersion}`;
  if (SCAN_META.astEnriched) {
    document.getElementById('meta-version').textContent +=
      ` · AST (${SCAN_META.astEnriched} Java)`;
  } else if (SCAN_META.totalComponents > 0) {
    document.getElementById('meta-version').textContent += ' · regex only';
  }
})();

// ── Warnings ──────────────────────────────────────────────────────────────────
(function() {
  if (!WARNINGS.length) return;
  const badge = document.getElementById('warnings-badge');
  badge.classList.add('has-warnings');
  document.getElementById('warnings-count').textContent = WARNINGS.length;
  const list = document.getElementById('warnings-list');
  WARNINGS.forEach(w => {
    const div = document.createElement('div');
    div.className = 'warning-item';
    div.textContent = w;
    list.appendChild(div);
  });
  badge.addEventListener('click', () => {
    document.getElementById('warnings-panel').classList.toggle('open');
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('#warnings-badge') && !e.target.closest('#warnings-panel')) {
      document.getElementById('warnings-panel').classList.remove('open');
    }
  });
})();

// ── External systems overlay ──────────────────────────────────────────────────
function renderExternalSystems() {
  // Build map: extName → {callers: [], endpoints: []}
  const extMap = {};
  Object.values(COMP).forEach(comp => {
    (comp.externalSystems || []).forEach(ext => {
      if (!extMap[ext]) extMap[ext] = {callers: new Set(), endpoints: []};
      extMap[ext].callers.add(comp.name);
    });
  });
  // Find which HTTP endpoints can reach each external (via comp dependencies)
  SIDEBAR_RAW.forEach(ctrl => {
    (ctrl.endpoints || []).forEach(ep => {
      // BFS through call chain
      const visited = new Set();
      const queue = [...(ep.calls || [])];
      queue.forEach(n => visited.add(n));
      let head = 0;
      while (head < queue.length) {
        const name = queue[head++];
        const c = COMP[name];
        if (!c) continue;
        (c.externalSystems || []).forEach(ext => {
          if (extMap[ext]) extMap[ext].endpoints.push(`${ep.method} ${ep.path}`);
        });
        (c.dependencies || []).forEach(dep => {
          if (!visited.has(dep)) { visited.add(dep); queue.push(dep); }
        });
      }
    });
  });

  const body = document.getElementById('ext-body');
  body.innerHTML = '';
  const sorted = Object.keys(extMap).sort();
  if (!sorted.length) {
    body.innerHTML = '<p style="color:var(--text-muted);padding:24px">No external systems detected.</p>';
    return;
  }
  sorted.forEach(ext => {
    const info = extMap[ext];
    const card = document.createElement('div');
    card.className = 'ext-card';
    const eps = [...new Set(info.endpoints)];
    card.innerHTML = `<h3>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#64748b" stroke-width="2"><circle cx="12" cy="12" r="10"/></svg>
        ${escHtml(ext)}
      </h3>
      <div class="ext-card-section">Called by (${info.callers.size})</div>
      ${[...info.callers].sort().map(c=>`<div class="ext-card-item">${escHtml(c)}</div>`).join('')}
      ${eps.length ? `<div class="ext-card-section">Possibly reachable from endpoints (${eps.length})</div>
      ${eps.slice(0,8).map(e=>`<div class="ext-card-item" style="font-family:monospace;font-size:11px">${escHtml(e)}</div>`).join('')}
      ${eps.length>8?`<div class="ext-card-item" style="color:var(--text-muted)">…and ${eps.length-8} more</div>`:''}` : ''}
    `;
    body.appendChild(card);
  });
}

document.getElementById('btn-ext-open').addEventListener('click', () => {
  renderExternalSystems();
  document.getElementById('ext-overlay').classList.add('open');
});
document.getElementById('btn-ext-close').addEventListener('click', () => {
  document.getElementById('ext-overlay').classList.remove('open');
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.getElementById('ext-overlay').classList.remove('open');
    document.getElementById('stats-overlay').classList.remove('open');
  }
});

// ── Stats overlay ─────────────────────────────────────────────────────────────
function renderStats() {
  const comps = Object.values(COMP);
  const sb = document.getElementById('stats-body');

  // Pre-compute fan-in (callers per component)
  const fanIn = {};
  comps.forEach(c => {
    (c.dependencies || []).forEach(dep => { fanIn[dep] = (fanIn[dep] || 0) + 1; });
  });

  const totalLoc = comps.reduce((s, c) => s + (c.loc || 0), 0);
  const totalEndpoints = comps.reduce((s, c) => s + (c.endpoints || []).length, 0);
  const totalScheduled = comps.reduce((s, c) => s + (c.nonHttpEntrypoints || []).filter(e => e.kind === 'SCHEDULED').length, 0);
  const totalKafka = comps.reduce((s, c) => s + (c.nonHttpEntrypoints || []).filter(e => e.kind === 'KAFKA').length, 0);
  const totalEvents = comps.reduce((s, c) => s + (c.nonHttpEntrypoints || []).filter(e => e.kind === 'EVENT').length, 0);
  const allExternals = new Set(comps.flatMap(c => c.externalSystems || []));
  const orphans = comps.filter(c => !c.endpoints?.length && !c.nonHttpEntrypoints?.length && !(fanIn[c.name] > 0));
  const domains = [...new Set(comps.map(c => c.domain).filter(Boolean))];

  // Kind breakdown
  const byKind = {};
  comps.forEach(c => { byKind[c.kind] = (byKind[c.kind] || 0) + 1; });
  const kindOrder = ['CONTROLLER','SERVICE','REPOSITORY','CLIENT','CONSUMER','MAPPER','CACHE','CONFIG','SCHEDULER','FACADE','VALIDATOR','COMPONENT'];

  function kpi(value, label) {
    return `<div class="stats-kpi"><div class="stats-kpi-value">${value}</div><div class="stats-kpi-label">${label}</div></div>`;
  }

  function table(headers, rows, onClickName) {
    const maxBar = Math.max(...rows.map(r => r.bar || 0), 1);
    return `<table class="stats-table">
      <thead><tr>${headers.map(h => `<th>${escHtml(h)}</th>`).join('')}</tr></thead>
      <tbody>${rows.slice(0,15).map(r => `<tr ${onClickName && r.name ? `onclick="showNodeDetail('${CSS.escape(r.name)}');document.getElementById('stats-overlay').classList.remove('open')"` : ''}>
        ${r.cells.map((cell, i) => `<td>${i === r.barCol ? `<div class="stats-bar-wrap"><span style="min-width:36px;font-family:monospace">${escHtml(String(cell))}</span><div class="stats-bar-track"><div class="stats-bar-fill" style="width:${Math.round((cell/maxBar)*100)}%"></div></div></div>` : escHtml(String(cell))}</td>`).join('')}
      </tr>`).join('')}</tbody>
    </table>`;
  }

  let html = '';

  // ── KPI row
  html += `<div class="stats-section">
    <div class="stats-section-title">Overview</div>
    <div class="stats-kpi-row">
      ${kpi(comps.length.toLocaleString(), 'Components')}
      ${kpi(totalLoc.toLocaleString(), 'Lines of code')}
      ${kpi(totalEndpoints, 'HTTP endpoints')}
      ${kpi(allExternals.size, 'External systems')}
      ${kpi(domains.length, 'Domains')}
      ${kpi(orphans.length, orphans.length > 0 ? '⚠ Orphan components' : 'Orphan components')}
    </div>
  </div>`;

  // ── Entrypoint breakdown
  html += `<div class="stats-section">
    <div class="stats-section-title">Entrypoints</div>
    <div class="stats-kpi-row">
      ${kpi(totalEndpoints, 'HTTP')}
      ${kpi(totalScheduled, '@Scheduled')}
      ${kpi(totalKafka, '@KafkaListener')}
      ${kpi(totalEvents, '@EventListener')}
    </div>
  </div>`;

  // ── Component kind breakdown
  const kindRows = kindOrder.filter(k => byKind[k]).map(k => ({
    cells: [k.charAt(0)+k.slice(1).toLowerCase(), byKind[k]],
    bar: byKind[k], barCol: 1,
  }));
  html += `<div class="stats-grid">
    <div class="stats-section">
      <div class="stats-section-title">Components by kind</div>
      ${table(['Kind', 'Count'], kindRows, false)}
    </div>`;

  // ── Domain distribution
  const domainRows = domains.map(d => {
    const n = comps.filter(c => c.domain === d).length;
    return { cells: [d, n], bar: n, barCol: 1 };
  }).sort((a, b) => b.bar - a.bar);
  html += `<div class="stats-section">
      <div class="stats-section-title">Components by domain</div>
      ${table(['Domain', 'Components'], domainRows, false)}
    </div>
  </div>`;

  // ── Largest files by LOC
  const locRows = [...comps].filter(c => c.loc > 0)
    .sort((a, b) => b.loc - a.loc)
    .map(c => ({ name: c.name, cells: [c.name, c.kind.charAt(0)+c.kind.slice(1).toLowerCase(), c.loc], bar: c.loc, barCol: 2 }));
  html += `<div class="stats-section">
    <div class="stats-section-title">Largest files (lines of code)</div>
    ${table(['Component', 'Kind', 'LOC'], locRows, true)}
  </div>`;

  // ── Highest fan-out (most dependencies — complexity risk)
  const fanOutRows = [...comps].sort((a, b) => (b.dependencies?.length||0) - (a.dependencies?.length||0))
    .filter(c => c.dependencies?.length)
    .map(c => ({ name: c.name, cells: [c.name, c.kind.charAt(0)+c.kind.slice(1).toLowerCase(), c.dependencies.length], bar: c.dependencies.length, barCol: 2 }));
  html += `<div class="stats-section">
    <div class="stats-section-title">Most dependencies (fan-out — complexity risk)</div>
    ${table(['Component', 'Kind', 'Deps'], fanOutRows, true)}
  </div>`;

  // ── Highest fan-in (most callers — blast radius)
  const fanInRows = Object.entries(fanIn).sort((a,b) => b[1]-a[1])
    .map(([name, n]) => {
      const c = COMP[name];
      const kind = c ? c.kind.charAt(0)+c.kind.slice(1).toLowerCase() : 'external';
      return { name, cells: [name, kind, n], bar: n, barCol: 2 };
    });
  html += `<div class="stats-section">
    <div class="stats-section-title">Most depended-on (fan-in — blast radius if changed)</div>
    ${table(['Component', 'Kind', 'Callers'], fanInRows, true)}
  </div>`;

  // ── Controllers with most endpoints
  const ctrlRows = comps.filter(c => c.endpoints?.length)
    .sort((a, b) => b.endpoints.length - a.endpoints.length)
    .map(c => ({ name: c.name, cells: [c.name, c.domain || '—', c.endpoints.length], bar: c.endpoints.length, barCol: 2 }));
  html += `<div class="stats-section">
    <div class="stats-section-title">Controllers by endpoint count</div>
    ${table(['Controller', 'Domain', 'Endpoints'], ctrlRows, true)}
  </div>`;

  // ── External system reach
  const extReach = {};
  comps.forEach(c => { (c.externalSystems||[]).forEach(e => { extReach[e] = (extReach[e]||0)+1; }); });
  const extRows = Object.entries(extReach).sort((a,b)=>b[1]-a[1])
    .map(([e, n]) => ({ cells: [e, n], bar: n, barCol: 1 }));
  html += `<div class="stats-section">
    <div class="stats-section-title">External systems — components that call each</div>
    ${table(['System', 'Callers'], extRows, false)}
  </div>`;

  // ── Orphan components
  if (orphans.length) {
    html += `<div class="stats-section">
      <div class="stats-section-title">⚠ Orphan components — no callers, no entrypoints</div>
      ${table(['Component', 'Kind', 'Package'], orphans.map(c => ({
        name: c.name,
        cells: [c.name, c.kind.charAt(0)+c.kind.slice(1).toLowerCase(), c.package],
      })), true)}
    </div>`;
  }

  sb.innerHTML = html;
}

document.getElementById('btn-stats-open').addEventListener('click', () => {
  renderStats();
  document.getElementById('stats-overlay').classList.add('open');
});
document.getElementById('btn-stats-close').addEventListener('click', () => {
  document.getElementById('stats-overlay').classList.remove('open');
});

renderSidebar();

// ── Watch mode: poll version.json for changes ─────────────────────────────────
(function(){
  let knownTs = null;
  let knownCount = null;
  const banner = document.getElementById('reload-banner');
  const detail = document.getElementById('reload-banner-detail');
  document.getElementById('btn-reload-dismiss').addEventListener('click', () => {
    banner.classList.remove('visible');
    // Update knownTs so it doesn't immediately reappear
    fetch('/version.json?_=' + Date.now())
      .then(r => r.json()).then(v => { knownTs = v.ts; knownCount = v.components; })
      .catch(() => {});
  });
  function poll() {
    fetch('/version.json?_=' + Date.now())
      .then(r => r.json())
      .then(v => {
        if (knownTs === null) { knownTs = v.ts; knownCount = v.components; return; }
        if (v.ts !== knownTs) {
          const diff = v.components !== knownCount
            ? ` — ${knownCount} → ${v.components} components`
            : '';
          detail.textContent = diff;
          banner.classList.add('visible');
        }
      })
      .catch(() => {}); // not served via HTTP, or file missing — silently skip
  }
  setInterval(poll, 15000);
  poll();
})();
</script>
</body>
</html>"""


# ── CLI ───────────────────────────────────────────────────────────────────────

def _resolve_source_root(given: Path) -> Path:
    """Walk down to the Spring source root if the user passes a repo/module root."""
    if not given.exists():
        print(f'Error: {given} does not exist', file=sys.stderr)
        sys.exit(1)
    # Try standard Spring Boot source layouts in preference order
    for candidate in [
        given / 'src' / 'main',
        given / 'src' / 'main' / 'kotlin',
        given / 'src' / 'main' / 'java',
    ]:
        if candidate.exists():
            print(f'Auto-detected source root: {candidate}', file=sys.stderr)
            return candidate
    # Passed path is already the source root (or an explicit non-standard layout)
    return given


def _generate_docs_index(docs_dir: Path, title: str, components: list[Component]) -> None:
    """Write an index.md into docs_dir that links to all endpoint files."""
    by_name = {c.name: c for c in components}
    lines: list[str] = [f'# {title}', '']

    # Summary line
    controllers = [c for c in components if c.endpoints]
    total_ep = sum(len(c.endpoints) for c in controllers)
    all_ext = sorted({e for c in components for e in c.external_systems})
    domains = sorted({c.domain for c in components if c.domain})
    lines += [
        f'> {len(components)} components · {total_ep} HTTP endpoints · '
        f'{len(domains)} domains · {len(all_ext)} external systems',
        '',
    ]

    # Endpoint index grouped by controller
    lines += ['## Endpoints', '']
    for ctrl in sorted(controllers, key=lambda c: c.name):
        domain_tag = f' `{ctrl.domain}`' if ctrl.domain else ''
        lines += [f'### {ctrl.name}{domain_tag}', '']
        lines += ['| Method | Path | Handler | File |']
        lines += ['|---|---|---|---|']
        for ep in ctrl.endpoints:
            slug = _slug(ep.http_method, ep.path)
            short_file = re.sub(r'.*/src/main/(kotlin|java)/', '', ctrl.file)
            lines.append(
                f'| [{ep.http_method}]({slug}.md) | `{ep.path}` | `{ep.handler}()` | `{short_file}` |'
            )
        lines.append('')

    # External systems summary
    if all_ext:
        lines += ['## External systems', '']
        ext_callers: dict[str, list[str]] = {}
        for c in components:
            for e in c.external_systems:
                ext_callers.setdefault(e, []).append(c.name)
        lines += ['| System | Called by |']
        lines += ['|---|---|']
        for ext in sorted(ext_callers):
            callers = ', '.join(f'`{c}`' for c in ext_callers[ext])
            lines.append(f'| **{ext}** | {callers} |')
        lines.append('')

    (docs_dir / 'index.md').write_text('\n'.join(lines), encoding='utf-8')


def _run_once(args, root: Path, html_path: Path) -> None:
    """Scan and write all requested outputs. Called on every watch iteration."""
    import datetime
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] Scanning {root}…', file=sys.stderr)
    components, scan_warnings, ast_enriched = scan(root)
    resolve(components)

    visible = [c for c in components if c.kind not in ('CONFIG',)]
    print(f'Found {len(visible)} components.', file=sys.stderr)

    if not visible:
        print('No Spring components detected. Is this a Spring Boot project?', file=sys.stderr)
        return

    if not args.no_html:
        import time as _time
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html = generate_html(visible, args.title, scan_root=root,
                             warnings=scan_warnings, ast_enriched=ast_enriched)
        html_path.write_text(html, encoding='utf-8')
        # Write version.json alongside index.html so the browser can poll for changes
        version_path = html_path.parent / 'version.json'
        version_path.write_text(
            json.dumps({'ts': int(_time.time()), 'components': len(visible)}),
            encoding='utf-8'
        )
        print(f'Written → {html_path}', file=sys.stderr)

    if not args.no_md:
        md = generate_markdown(visible)
        Path(args.md).write_text(md, encoding='utf-8')
        print(f'Written → {args.md}', file=sys.stderr)

    if args.docs:
        docs_dir = Path(args.docs)
        n = generate_endpoint_docs(visible, docs_dir, title=args.title)
        _generate_docs_index(docs_dir, args.title, visible)
        print(f'Written → {docs_dir}/ ({n} endpoint files + index.md)', file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser(
        description='Generate an interactive Spring Boot architecture map.'
    )
    p.add_argument('root', nargs='?', default='.', help='Source root (default: .)')
    p.add_argument('--html', default='appmap.html', help='HTML output (default: appmap.html)')
    p.add_argument('--md', default='architecture.md', help='Markdown output (default: architecture.md)')
    p.add_argument('--title', default='Application Map', help='Map title')
    p.add_argument('--docs', default='', metavar='DIR', help='Write one markdown file per endpoint into DIR')
    p.add_argument('--no-html', action='store_true', help='Skip HTML generation')
    p.add_argument('--no-md',   action='store_true', help='Skip Markdown generation')
    p.add_argument('--list', action='store_true', help='Print component table to stdout')
    p.add_argument('--serve', action='store_true', help='Serve HTML via localhost and open in browser')
    p.add_argument('--port', type=int, default=8742, help='Port for --serve (default: 8742)')
    p.add_argument('--watch', action='store_true', help='Re-scan every --interval seconds')
    p.add_argument('--interval', type=int, default=120, help='Watch interval in seconds (default: 120)')
    args = p.parse_args()

    root = _resolve_source_root(Path(args.root))

    if args.list:
        components, _, _ = scan(root)
        resolve(components)
        visible = [c for c in components if c.kind not in ('CONFIG',)]
        print(f'{"Component":<42} {"Kind":<12} {"Domain":<18} {"External"}')
        print('─' * 90)
        for c in sorted(visible, key=lambda x: (x.domain, x.kind, x.name)):
            ext = ', '.join(c.external_systems) or '—'
            print(f'{c.name:<42} {c.kind:<12} {c.domain or "—":<18} {ext}')
        return

    _SERVE_DIR = Path.home() / '.claude' / 'skills' / 'codemap' / 'serve'
    html_path = Path(args.html) if args.html != 'appmap.html' else _SERVE_DIR / 'index.html'

    if args.serve and not args.no_html:
        import http.server, threading, webbrowser, functools, time, socket, signal, os
        serve_dir = str(html_path.parent.resolve())
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=serve_dir)
        handler.log_message = lambda *a: None  # type: ignore
        port = args.port
        try:
            result = subprocess.run(['lsof', '-ti', f'tcp:{port}'], capture_output=True, text=True)
            for pid in result.stdout.strip().split():
                try: os.kill(int(pid), signal.SIGTERM)
                except Exception: pass
            time.sleep(0.3)
        except Exception:
            pass
        try:
            server = http.server.HTTPServer(('localhost', port), handler)
        except OSError:
            print(f'Could not bind to port {port}', file=sys.stderr)
            sys.exit(1)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://localhost:{port}/'
        print(f'Serving at {url}', file=sys.stderr)
        webbrowser.open(url)

        _run_once(args, root, html_path)
        if args.watch:
            print(f'Watching — re-scanning every {args.interval}s. Ctrl+C to stop.', file=sys.stderr)
            try:
                while True:
                    time.sleep(args.interval)
                    _run_once(args, root, html_path)
            except KeyboardInterrupt:
                pass
        else:
            try:
                while True: time.sleep(1)
            except KeyboardInterrupt:
                pass
        server.shutdown()
    else:
        import time
        _run_once(args, root, html_path)
        if args.watch:
            print(f'Watching — re-scanning every {args.interval}s. Ctrl+C to stop.', file=sys.stderr)
            try:
                while True:
                    time.sleep(args.interval)
                    _run_once(args, root, html_path)
            except KeyboardInterrupt:
                pass


if __name__ == '__main__':
    main()
