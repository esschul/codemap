import re
import sys
import json
import subprocess
from pathlib import Path
from typing import Optional
from collections import defaultdict

from .model import Component, Endpoint, NonHttpEntrypoint

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
    (r'JpaRepository|CrudRepository|MongoRepository|R2dbcRepository|JdbcTemplate|NamedParameterJdbcTemplate|EntityManager|PersistenceContext', 'database'),
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


# ── AST scanners ──────────────────────────────────────────────────────────────

# Bundled JARs live alongside this module (works both in-repo and when pip-installed)
_AST_JAR = Path(__file__).parent / 'ast-scanner.jar'
_KT_JAR  = Path(__file__).parent / 'kt-scanner.jar'

# Cache: file path → parsed result dict (or None on error)
_ast_cache: dict[str, Optional[dict]] = {}


def _run_jar_scanner(jar: Path, paths: list[Path]) -> dict[str, dict]:
    """Invoke a stdin-to-JSON JAR scanner. Returns {file_path_str: result_dict}."""
    if not jar.exists():
        return {}
    try:
        proc = subprocess.run(
            ['java', '-jar', str(jar)],
            input='\n'.join(str(p) for p in paths),
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return {}
        results = json.loads(proc.stdout)
        return {r['file']: r for r in results if r}
    except Exception:
        return {}


def _ast_scan_files(java_paths: list[Path]) -> dict[str, dict]:
    """JavaParser-based scanner for .java files."""
    return _run_jar_scanner(_AST_JAR, java_paths)


def _kt_scan_files(kt_paths: list[Path]) -> dict[str, dict]:
    """tree-sitter-based scanner for .kt files."""
    return _run_jar_scanner(_KT_JAR, kt_paths)


def _apply_ast_result(comp: Component, ast: dict, base_path: str,
                      preserve_on_empty: bool = False) -> None:
    """
    Overwrite field_map, dependencies, and endpoint call lists with AST-accurate data.

    preserve_on_empty: when True, keep the existing regex-derived field_map and
    dependencies if the AST returns no fields. Use this for Kotlin, where some
    constructor patterns are not yet recognised by the grammar. For Java, pass
    False so that the JavaParser scanner can authoritatively say "no fields here"
    and clear out regex false positives.
    """
    field_map: dict[str, str] = {}
    for f in ast.get('fields', []):
        field_map[f['name']] = f['type']

    if field_map or not preserve_on_empty:
        comp.field_map = field_map
        comp.dependencies = list(dict.fromkeys(field_map.values()))

    # Resolve field names → type names for endpoint call lists.
    # When AST found no fields but preserve_on_empty is True, fall back to the
    # regex-derived field_map so ep.calls is not silently cleared.
    effective_field_map = field_map if field_map else (comp.field_map or {})
    method_calls: dict[str, list[str]] = {}
    method_field_calls: dict[str, list[dict]] = {}
    for m in ast.get('methods', []):
        called_fields = m.get('callsOnFields', [])
        called_types = list(dict.fromkeys(
            effective_field_map[fn] for fn in called_fields if fn in effective_field_map
        ))
        method_calls[m['name']] = called_types
        method_field_calls[m['name']] = m.get('fieldCalls', [])

    comp.method_field_calls = method_field_calls

    for ep in comp.endpoints:
        if ep.handler in method_calls:
            ep.calls = method_calls[ep.handler]
        if ep.handler in method_field_calls:
            ep.field_calls = method_field_calls[ep.handler]


# ── File parser ───────────────────────────────────────────────────────────────

def parse_file(path: Path, skip_annotation_defs: bool = False) -> Optional[Component]:
    path_str = str(path).replace('\\', '/')
    if any(f in path_str for f in SKIP_FRAGMENTS):
        return None

    is_java = path.suffix == '.java'
    text = path.read_text(encoding='utf-8', errors='replace')

    # Skip the Spring Boot main application class — not an architectural component
    if '@SpringBootApplication' in text:
        return None

    # Skip annotation definition files — they are meta-annotations, not components
    if skip_annotation_defs:
        if is_java and re.search(r'(?:public\s+)?@interface\s+\w+', text):
            return None
        if not is_java and re.search(r'\bannotation\s+class\s+\w+', text):
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

    # Extract implemented interfaces from the class declaration line
    decl_line = text[class_pos:class_pos + 300].split('{')[0]
    implemented_ifaces = []
    if is_java:
        # Java: class Foo implements Bar, Baz
        m2 = re.search(r'\bimplements\s+([\w\s,<>]+?)(?:\s+extends|\s*$)', decl_line)
        if m2:
            implemented_ifaces = [re.sub(r'<.*?>', '', i).strip() for i in m2.group(1).split(',') if i.strip()]
    else:
        # Kotlin: class Foo(…) : Bar, Baz(…) — supertypes after the colon
        # Skip the constructor params first, then grab everything after ':'
        m2 = re.search(r'\)\s*:\s*(.*)', decl_line) or re.search(r'class\s+\w+\s*:\s*(.*)', decl_line)
        if m2:
            # Each supertype may have constructor args: Bar(…) or just Bar
            for entry in re.split(r',\s*', m2.group(1)):
                name = re.sub(r'<.*?>', '', entry.split('(')[0]).strip()
                if re.match(r'^[A-Z]\w+$', name):  # only UpperCamelCase → interfaces/classes
                    implemented_ifaces.append(name)

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
    body_snippet = supertype_snippet + text[class_pos:class_pos + 2000]
    externals = _infer_externals(all_types, body_snippet)

    # Repositories that extend an abstract JPA/DAO base class won't declare
    # EntityManager themselves — infer database from the supertype name.
    if kind == 'REPOSITORY' and 'database' not in externals:
        if re.search(r'\bextends\s+\w*(Jpa|Jdbc|Abstract\w*Dao|Abstract\w*Repository)\w*', body_snippet, re.IGNORECASE):
            externals.append('database')

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
        implements=implemented_ifaces,
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
                # @RequestMapping defaults to ANY — resolve from method = RequestMethod.XXX
                # or method = GET (static import)
                if http_method == 'ANY' and ann_body_str:
                    rm = re.search(
                        r'method\s*=\s*(?:\[?\s*)?(?:RequestMethod\.)?'
                        r'(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|TRACE)',
                        ann_body_str,
                    )
                    if rm:
                        http_method = rm.group(1).upper()

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


# Matches a Java @interface declaration
_JAVA_IFACE_RE = re.compile(r'(?:public\s+)?@interface\s+(\w+)', re.MULTILINE)
# Matches a Kotlin annotation class declaration
_KT_ANN_CLASS_RE = re.compile(r'annotation\s+class\s+(\w+)', re.MULTILINE)
# Extracts annotation names from preamble text (e.g. @RestController, @Target(...))
_ANN_NAME_RE = re.compile(r'@(\w+)')


def _discover_composed_annotations(files: list[Path]) -> dict[str, str]:
    """
    Scan Java/Kotlin files for custom annotation definitions that are themselves
    meta-annotated with a known role annotation (e.g. @RestController on @interface
    RestApiController). Inspects the full preamble before each declaration so that
    @Target, @Retention, @RestController ordering doesn't matter.
    Returns {custom_name: role} to merge into ROLE_ANNOTATIONS.
    """
    discovered: dict[str, str] = {}
    for f in files:
        if f.suffix not in ('.java', '.kt'):
            continue
        try:
            text = f.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if '@interface' not in text and 'annotation class' not in text:
            continue

        decl_re = _JAVA_IFACE_RE if f.suffix == '.java' else _KT_ANN_CLASS_RE
        for m in decl_re.finditer(text):
            name = m.group(1)
            if name in ROLE_ANNOTATIONS or name in discovered:
                continue
            # Inspect the preamble: everything from the previous declaration (or
            # start of file) up to this declaration.
            preamble_start = max(0, text.rfind('\n\n', 0, m.start()))
            preamble = text[preamble_start:m.start()]
            for ann_m in _ANN_NAME_RE.finditer(preamble):
                meta = ann_m.group(1)
                if meta in ROLE_ANNOTATIONS:
                    discovered[name] = ROLE_ANNOTATIONS[meta]
                    break
    return discovered


def scan(root: Path) -> tuple[list[Component], list[str], int]:
    """Returns (components, warnings, ast_enriched_count)."""
    components: list[Component] = []
    warnings: list[str] = []
    parse_errors = 0
    kt_files = sorted(root.rglob('*.kt'))
    java_files = sorted(root.rglob('*.java'))
    all_files = kt_files + java_files
    if not all_files:
        print(
            f'Error: no Kotlin or Java files found under {root}\n'
            'codemap only supports Spring Boot (Kotlin/Java) projects.',
            file=sys.stderr,
        )
        sys.exit(1)

    # Pre-pass: find composed/meta-annotations and temporarily extend role map.
    # We restore the global after the scan so watch-mode scans of different
    # projects don't leak each other's custom annotations.
    composed = _discover_composed_annotations(all_files)
    _added_keys: list[str] = []
    if composed:
        for k, v in composed.items():
            if k not in ROLE_ANNOTATIONS:
                ROLE_ANNOTATIONS[k] = v
                _added_keys.append(k)
        print(f'  Discovered composed annotations: {", ".join(f"@{k}" for k in _added_keys)}', file=sys.stderr)

    try:
        for src_file in sorted(all_files, key=lambda p: str(p)):
            try:
                comp = parse_file(src_file, skip_annotation_defs=True)
                if comp:
                    components.append(comp)
            except Exception as e:
                parse_errors += 1
                warnings.append(f'Could not parse {src_file.name}: {e}')
    finally:
        for k in _added_keys:
            ROLE_ANNOTATIONS.pop(k, None)

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
                    _apply_ast_result(comp, ast, comp.base_path, preserve_on_empty=False)
            ast_enriched = len(ast_results)
            print(f'  AST: enriched {ast_enriched} Java components.', file=sys.stderr)
    elif java_comps:
        warnings.append('Java AST scanner JAR not found — using regex fallback for Java files')

    # AST pass: re-scan Kotlin files with tree-sitter for accurate field types + method calls
    kt_comps = [c for c in components if c.file.endswith('.kt')]
    if kt_comps and _KT_JAR.exists():
        print('Running AST scanner on Kotlin files…', file=sys.stderr)
        kt_results = _kt_scan_files([Path(c.file) for c in kt_comps])
        if kt_results:
            for comp in kt_comps:
                ast = kt_results.get(comp.file)
                if ast:
                    _apply_ast_result(comp, ast, comp.base_path, preserve_on_empty=True)
            ast_enriched += len(kt_results)
            print(f'  AST: enriched {len(kt_results)} Kotlin components.', file=sys.stderr)

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
                        updated.append(Endpoint(http_method=info['method'], path=info['path'],
                                                handler=fn_name, calls=ep.calls,
                                                field_calls=ep.field_calls))
                    # drop endpoints with no matching route (private helpers etc.)
                else:
                    updated.append(ep)
            comp.endpoints = updated

    return components, warnings, ast_enriched
