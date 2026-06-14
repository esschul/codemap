import re
import json
import subprocess
from collections import defaultdict
from pathlib import Path

from .model import Component


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


def _sidebar_data(components: list[Component], by_name: dict | None = None,
                  iface_map: dict | None = None) -> list[dict]:
    from .evidence import build_evidence
    _by_name = by_name or {c.name: c for c in components}
    _iface_map = iface_map or {}
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
                    'path': ep.path if not ep.path.startswith('__handler__') else f'(handler) {ep.handler}',
                    'handler': ep.handler,
                    'calls': ep.calls,
                    'fieldCalls': ep.field_calls,
                    'flow': build_evidence(ep, comp, _by_name, iface_map=_iface_map),
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
                {'method': ep.http_method, 'path': ep.path, 'handler': ep.handler,
                 'calls': ep.calls, 'fieldCalls': ep.field_calls, 'flow': []}
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
                  ast_enriched: int = 0, iface_map: dict | None = None) -> str:
    from .evidence import build_evidence
    by_name = {c.name: c for c in components}
    _iface_map = iface_map or {}
    nodes, edges = _build_graph_data(components)
    sidebar = _sidebar_data(components, by_name=by_name, iface_map=_iface_map)
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
  --arrow:#374151;--accent:#3b82f6;
}
body.light{
  --bg:#f1f5f9;--surface:#ffffff;--border:#e2e8f0;--text:#0f172a;
  --text-dim:#64748b;--text-muted:#94a3b8;--text-code:#334155;
  --hover:#f8fafc;--active-outline:#2563eb;--chip-bg:#dbeafe;
  --chip-border:#93c5fd;--chip-text:#1d4ed8;--input-bg:#f8fafc;
  --scrollbar:#cbd5e1;--detail-label:#94a3b8;--detail-val:#334155;
  --arrow:#cbd5e1;--accent:#2563eb;
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
.inspector{width:300px;flex-shrink:0;border-left:1px solid var(--border);
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
.ext-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px;overflow:hidden}
.ext-card h3{font-size:13px;font-weight:700;color:var(--text);margin-bottom:8px;
  display:flex;align-items:center;gap:8px}
.ext-card-section{font-size:10px;font-weight:700;color:var(--text-muted);
  text-transform:uppercase;letter-spacing:.06em;margin:8px 0 4px}
.ext-card-item{font-size:12px;color:var(--text-dim);padding:2px 0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

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
    <rect x="${PAD_X}" y="13" width="${ep.method.length * 7 + 20}" height="22" rx="4" fill="${mc}22" stroke="${mc}" stroke-width="1.5"/>
    <text x="${PAD_X + 10}" y="29" font-family="system-ui" font-size="11" font-weight="800" fill="${mc}">${ep.method}</text>
    <text x="${PAD_X + ep.method.length * 7 + 36}" y="29" font-family="'SF Mono','Fira Code',monospace" font-size="12" fill="#94a3b8">${escXml(ep.path)}</text>
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

// ── Ollama discovery ──────────────────────────────────────────────────────────
let ollamaModel = null;
let ollamaReady = false;  // true once discovery finishes (model found or not)
const ollamaReadyCallbacks = [];
function onOllamaReady(fn) {
  if (ollamaReady) fn(ollamaModel);
  else ollamaReadyCallbacks.push(fn);
}
(async () => {
  try {
    const r = await fetch('http://localhost:11434/api/tags',
      { signal: AbortSignal.timeout(3000) });
    if (r.ok) {
      const data = await r.json();
      const models = (data.models || []).map(m => m.name);
      const preferred = ['llama3', 'llama3.2', 'mistral', 'phi3', 'phi', 'gemma', 'qwen'];
      ollamaModel = models.find(n => preferred.some(p => n.startsWith(p))) || models[0] || null;
    }
  } catch { /* Ollama not running — silent */ }
  ollamaReady = true;
  ollamaReadyCallbacks.forEach(fn => fn(ollamaModel));
})();

function buildEvidencePrompt(ctrlName, ep) {
  const comp = COMP[ctrlName] || {};
  const extSystems = (comp.externalSystems || []).join(', ') || 'none';

  // Build call flow from bounded evidence graph (ep.flow), fall back to fieldCalls
  let flowLines = '';
  if ((ep.flow || []).length > 0) {
    flowLines = (ep.flow || []).map(step =>
      step.external
        ? `  ${step.from}  →  [${step.external}]`
        : `  ${step.from}  →  ${step.to}()`
    ).join('\\n');
  } else {
    flowLines = (ep.fieldCalls || [])
      .map(fc => `  ${fc.field}.${fc.method}() [${fc.type}]`)
      .join('\\n') || '  (none detected)';
  }

  return (
    `You are a technical documentation assistant for a Spring Boot application.\\n` +
    `Based only on the evidence below, write 1-2 sentences describing what this endpoint does.\\n` +
    `Use functional language (validate, create, persist, publish, fetch, calculate, notify).\\n` +
    `Do not mention class names unless necessary. Do not invent behaviour not supported by the evidence.\\n` +
    `If evidence is weak, phrase cautiously.\\n\\n` +
    `Endpoint: ${ep.method} ${ep.path}\\n` +
    `Handler: ${ctrlName}.${ep.handler}\\n` +
    `Call flow:\\n${flowLines}\\n` +
    `External systems: ${extSystems}\\n\\n` +
    `Description:`
  );
}

async function summariseEndpoint(ctrlName, ep, outputEl) {
  outputEl.textContent = '…';
  if (!ollamaReady) {
    await new Promise(resolve => onOllamaReady(() => resolve()));
  }
  if (!ollamaModel) {
    outputEl.textContent = 'Ollama not available. Start it with: ollama serve';
    return;
  }
  outputEl.textContent = `Using ${ollamaModel}…`;
  try {
    const resp = await fetch('http://localhost:11434/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: ollamaModel,
        prompt: buildEvidencePrompt(ctrlName, ep),
        stream: true,
      }),
    });
    outputEl.textContent = '';
    outputEl.style.display = 'none';
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += dec.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        if (!line.trim()) continue;
        const chunk = JSON.parse(line);
        if (chunk.response) {
          if (!outputEl.style.display || outputEl.style.display === 'none') outputEl.style.display = '';
          outputEl.textContent += chunk.response;
        }
      }
    }
  } catch (e) {
    outputEl.textContent = `Error: ${e.message}`;
  }
}

// ── Endpoint selection ────────────────────────────────────────────────────────
function selectEndpoint(ctrlName, ep, itemEl){
  document.querySelectorAll('.ep-item').forEach(el=>el.classList.remove('active'));
  if(itemEl) itemEl.classList.add('active');

  // Render SVG chain
  const chainSvg = document.getElementById('chain-svg');
  chainSvg.innerHTML = renderChainSVG(ctrlName, ep);
  document.getElementById('chain-empty').classList.add('hidden');

  // Detail panel
  // Bottom strip: just the endpoint path label
  const panel = document.getElementById('detail');
  panel.classList.remove('hidden');
  const noCallsNote = !ep.calls.length
    ? '<span style="opacity:.5;font-size:11px">— no service calls detected in handler body</span>' : '';
  panel.innerHTML = `
    <div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap">
      <span class="method m-${escHtml(ep.method)}" style="font-size:10px">${escHtml(ep.method)}</span>
      <code style="font-size:13px;color:var(--text)">${escHtml(ep.path)}</code>
      ${noCallsNote}
    </div>`;

  // Inspector panel: show call evidence + Summarise button
  const hasMethodEvidence = (ep.fieldCalls || []).length > 0;
  const hasEvidence = hasMethodEvidence;
  const callRows = (ep.fieldCalls || [])
    .map(fc => `<div style="font-size:11px;color:var(--text-muted);padding:2px 0;font-family:'SF Mono','Fira Code',monospace">
      <span style="color:var(--text)">${escHtml(fc.field)}</span>.${escHtml(fc.method)}()
      <span style="opacity:.5"> ${escHtml(fc.type)}</span>
    </div>`).join('');
  const ndBody = document.getElementById('nd-body');
  ndBody.innerHTML = `
    <div style="padding:12px 14px">
      <div style="font-size:10px;font-weight:700;color:var(--text-muted);
                  text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">
        ${escHtml(ep.method)} ${escHtml(ep.path)}
      </div>
      <div style="font-size:11px;color:var(--text-muted);margin-bottom:10px">
        handler: <code style="color:var(--text)">${escHtml(ctrlName)}.${escHtml(ep.handler)}()</code>
      </div>
      ${hasMethodEvidence ? `<div style="font-size:10px;font-weight:700;color:var(--text-muted);
        text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Calls</div>
        <div style="margin-bottom:12px">${callRows}</div>` : ''}
      ${hasEvidence ? `<button id="btn-summarise" style="
        width:100%;padding:7px 0;font-size:12px;font-weight:600;cursor:pointer;
        background:var(--accent);color:#fff;border:none;border-radius:6px;
        letter-spacing:.02em">
        ✦ Summarise with AI
      </button>
      <div id="llm-output" style="
        margin-top:12px;font-size:13px;line-height:1.75;color:var(--text);
        white-space:pre-wrap;border-top:1px solid var(--border);padding-top:12px;
        display:none"></div>` : ''}
    </div>`;

  // Expand inspector if collapsed
  const insp = document.getElementById('inspector');
  if (insp.classList.contains('collapsed')) insp.classList.remove('collapsed');

  if (hasEvidence) {
    const btn = document.getElementById('btn-summarise');
    btn.addEventListener('click', () => {
      btn.disabled = true;
      btn.style.opacity = '.6';
      summariseEndpoint(ctrlName, ep, document.getElementById('llm-output'))
        .finally(() => { if (btn.isConnected) { btn.disabled = false; btn.style.opacity = '1'; } });
    });
  }
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
      ` · AST (${SCAN_META.astEnriched})`;
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
