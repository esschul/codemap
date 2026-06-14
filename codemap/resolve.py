import re
from typing import Optional

from .model import Component


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

    # Build interface→implementations map from explicit `implements` declarations
    _iface_map: dict[str, list[str]] = {}
    for comp in components:
        for iface in comp.implements:
            _iface_map.setdefault(iface, []).append(comp.name)

    def _resolve_dep(dep: str) -> str:
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
