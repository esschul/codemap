import re
from collections import defaultdict
from pathlib import Path

from .model import Component

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
            for ext in c.external_systems:
                ext_callers.setdefault(ext, []).append(c.name)
        for ext in sorted(ext_callers):
            callers = ', '.join(f'`{n}`' for n in ext_callers[ext])
            lines.append(f'- **{ext}** — {callers}')
        lines.append('')

    (docs_dir / 'index.md').write_text('\n'.join(lines), encoding='utf-8')


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
