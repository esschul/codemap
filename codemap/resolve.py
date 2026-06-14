import re
from dataclasses import dataclass, field as dc_field
from typing import Optional

from .model import Component


@dataclass
class ResolveDiagnostics:
    """Collected during resolve() — describes what was dropped or ambiguous."""
    dropped: list[dict] = dc_field(default_factory=list)      # {comp, type, reason}
    ambiguous: list[dict] = dc_field(default_factory=list)    # {comp, type, implementations}
    no_field_calls: list[str] = dc_field(default_factory=list)  # endpoint handler names
    unreachable: list[str] = dc_field(default_factory=list)   # component names not reachable from any endpoint


def resolve(components: list[Component], diagnostics: Optional[ResolveDiagnostics] = None) -> dict[str, str]:
    """Returns iface_map: interface name → single concrete implementation name (where unambiguous)."""
    """Trim dependency lists to only reference other known components.
    Also resolves interface names to their implementations via FooImpl heuristic.
    Unresolvable dependencies that look like external clients are added as external systems."""
    known = {c.name for c in components}
    diag = diagnostics  # may be None

    # Suffixes that suggest an external service boundary rather than a utility type
    _EXTERNAL_SUFFIXES = ('Service', 'Client', 'Gateway', 'Adapter', 'Api', 'Provider')
    _NOISE_TYPES = frozenset({
        'String', 'Integer', 'Long', 'Boolean', 'List', 'Map', 'Set', 'Optional',
        'ObjectMapper', 'Logger', 'Duration', 'Cache', 'AtomicBoolean', 'BiConsumer',
        'DataSource', 'Environment', 'ApplicationAvailability',
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
                for dp in _LABEL_DROP_PREFIXES:
                    if prefix.startswith(dp) and len(prefix) > len(dp):
                        prefix = prefix[len(dp):]
                label = re.sub(r'(?<=[a-z])(?=[A-Z])', '-', prefix).lower()
                if label.count('-') >= 3:
                    return None
                return label
        return None

    # Build interface→implementations map from explicit `implements` declarations
    _iface_map: dict[str, list[str]] = {}
    for comp in components:
        for iface in comp.implements:
            _iface_map.setdefault(iface, []).append(comp.name)

    def _resolve_dep(dep: str, comp_name: str) -> str:
        if dep in known:
            return dep
        for suffix in ('Impl', 'Implementation'):
            candidate = dep + suffix
            if candidate in known:
                return candidate
        if dep in _iface_map:
            impls = _iface_map[dep]
            if len(impls) == 1:
                return impls[0]
            if diag is not None:
                diag.ambiguous.append({'comp': comp_name, 'type': dep, 'implementations': impls})
        return dep

    for comp in components:
        resolved = []
        for d in comp.dependencies:
            r = _resolve_dep(d, comp.name)
            if r in known and r != comp.name:
                resolved.append(r)
            elif r not in known:
                label = _to_external_label(d)
                if label and label not in comp.external_systems:
                    comp.external_systems.append(label)
                elif diag is not None and not label and d not in _NOISE_TYPES:
                    diag.dropped.append({'comp': comp.name, 'type': d, 'reason': 'unresolved — not a known component or external pattern'})
        comp.dependencies = list(dict.fromkeys(resolved))

        for ep in comp.endpoints:
            ep.calls = [c for c in ep.calls if c in known]
            if diag is not None and not ep.field_calls:
                diag.no_field_calls.append(f'{comp.name}.{ep.handler}  {ep.http_method} {ep.path}')

    if diag is not None:
        # Components not reachable from any endpoint (via BFS through dependencies)
        reachable: set[str] = set()
        by_name = {c.name: c for c in components}
        queue = [ep_comp.name
                 for ep_comp in components if ep_comp.endpoints
                 for _ in ep_comp.endpoints]
        # BFS from all controllers
        frontier = [c.name for c in components if c.endpoints]
        visited: set[str] = set(frontier)
        while frontier:
            nxt = []
            for name in frontier:
                for dep in by_name.get(name, Component('', '', '', '')).dependencies:
                    if dep not in visited:
                        visited.add(dep)
                        nxt.append(dep)
            frontier = nxt
        reachable = visited
        diag.unreachable = [
            c.name for c in components
            if c.name not in reachable and c.kind not in ('CONFIG',) and not c.endpoints
        ]

    # Return flat iface → impl map (only unambiguous entries)
    # Callers that need the ambiguous map can pass a ResolveDiagnostics and read diag.ambiguous,
    # or use resolve_full() which returns both maps.
    return {iface: impls[0] for iface, impls in _iface_map.items() if len(impls) == 1}


def resolve_full(
    components: list[Component],
    diagnostics: Optional[ResolveDiagnostics] = None,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Like resolve(), but also returns ambiguous_ifaces: {iface: [impl1, impl2, ...]}.

    Uses its own ResolveDiagnostics instance to capture ambiguous entries; merges into
    the caller-supplied diagnostics if provided.
    """
    _diag = ResolveDiagnostics()
    iface_map = resolve(components, diagnostics=_diag)
    if diagnostics is not None:
        diagnostics.dropped.extend(_diag.dropped)
        diagnostics.ambiguous.extend(_diag.ambiguous)
        diagnostics.no_field_calls.extend(_diag.no_field_calls)
        diagnostics.unreachable.extend(_diag.unreachable)
    ambiguous = {a['type']: a['implementations'] for a in _diag.ambiguous}
    return iface_map, ambiguous


def format_diagnostics(diag: ResolveDiagnostics) -> str:
    lines = ['╔══ Resolve Diagnostics ══════════════════════════════════════╗', '']

    if diag.ambiguous:
        lines.append('  AMBIGUOUS INTERFACES  (multiple implementations — picked none)')
        for a in diag.ambiguous:
            lines.append(f'    {a["comp"]}  →  {a["type"]}')
            for impl in a['implementations']:
                lines.append(f'        ↳ {impl}')
        lines.append('')

    if diag.dropped:
        lines.append('  DROPPED DEPENDENCIES  (not resolved to any component or external)')
        for d in diag.dropped:
            lines.append(f'    {d["comp"]}  →  {d["type"]}')
        lines.append('')

    if diag.no_field_calls:
        lines.append('  ENDPOINTS WITHOUT fieldCalls  (method-level evidence unavailable)')
        for ep in diag.no_field_calls:
            lines.append(f'    {ep}')
        lines.append('')

    if diag.unreachable:
        lines.append('  UNREACHABLE COMPONENTS  (not connected to any endpoint)')
        for name in diag.unreachable:
            lines.append(f'    {name}')
        lines.append('')

    if not any([diag.ambiguous, diag.dropped, diag.no_field_calls, diag.unreachable]):
        lines.append('  No issues found.')
        lines.append('')

    lines.append('╚═════════════════════════════════════════════════════════════╝')
    return '\n'.join(lines)
