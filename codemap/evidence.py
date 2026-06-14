"""
Bounded call graph evidence for an endpoint.

Traverses fieldCalls transitively across known Spring components,
stopping at REPOSITORY/CLIENT boundaries or max_depth hops.

Known limitations (documented, not fixed):
- Overloaded private helpers: keyed by name only; last one wins.
- Simple class names only: two components with the same name in different
  packages will collide in by_name.
- max_depth=2 by default: facades with 3+ layers may be truncated.
"""
from __future__ import annotations
from .model import Component, Endpoint

_STOP_KINDS = frozenset({'REPOSITORY', 'CLIENT', 'CACHE', 'MAPPER'})


def build_evidence(
    ep: Endpoint,
    ctrl: Component,
    by_name: dict[str, Component],
    max_depth: int = 2,
    iface_map: dict[str, str] | None = None,
    ambiguous_ifaces: dict[str, list[str]] | None = None,
) -> list[dict]:
    """
    Returns a list of flow steps:
      {"from": "A.method", "to": "B.method"}          normal hop
      {"from": "B", "external": "database"}            external system
      {"from": "A.method", "ambiguous": "IFace",       unresolvable interface
       "implementations": ["ImplA", "ImplB"]}
      {"from": "A.method", "truncated": True,          depth limit reached
       "depth": 2}
    """
    flow: list[dict] = []
    seen: set[str] = set()

    def _expand(comp: Component, method: str, depth: int) -> None:
        key = f'{comp.name}.{method}'
        if key in seen:
            return
        if depth > max_depth:
            flow.append({'from': key, 'truncated': True, 'depth': max_depth})
            return
        seen.add(key)

        calls = comp.method_field_calls.get(method, [])

        for fc in calls:
            field_type = fc.get('type', '')
            called_method = fc.get('method', '')

            # Resolve interface → concrete component
            resolved_type = field_type
            if field_type not in by_name and iface_map:
                resolved_type = iface_map.get(field_type, field_type)
            target = by_name.get(resolved_type)

            if target is None:
                # Check if this is an ambiguous interface (multiple implementations)
                if ambiguous_ifaces and field_type in ambiguous_ifaces:
                    step_key = f'{comp.name}.{method}→?{field_type}'
                    if step_key not in seen:
                        seen.add(step_key)
                        flow.append({
                            'from': f'{comp.name}.{method}',
                            'ambiguous': field_type,
                            'implementations': ambiguous_ifaces[field_type],
                        })
                # Unknown type — not a known component, already handled as external
                continue

            step_key = f'{comp.name}.{method}→{target.name}.{called_method}'
            if step_key in seen:
                continue
            seen.add(step_key)

            flow.append({
                'from': f'{comp.name}.{method}',
                'to': f'{target.name}.{called_method}',
            })

            for ext in target.external_systems:
                flow.append({'from': target.name, 'external': ext})

            if target.kind not in _STOP_KINDS:
                _expand(target, called_method, depth + 1)

    _expand(ctrl, ep.handler, 0)
    return flow


def collect_downstream_externals(flow: list[dict]) -> list[str]:
    """Extract all external system names that appear in a flow."""
    return list(dict.fromkeys(
        s['external'] for s in flow if 'external' in s
    ))


def format_evidence_text(ep: Endpoint, ctrl: Component, flow: list[dict],
                         comp_externals: list[str] | None = None) -> str:
    """Compact text representation for use in LLM prompts."""
    lines = [f'Endpoint: {ep.http_method} {ep.path}']
    lines.append(f'Handler:  {ctrl.name}.{ep.handler}()')

    if not flow:
        lines.append('Call flow: (no method-level evidence)')
    else:
        lines.append('Call flow:')
        for step in flow:
            if 'external' in step:
                lines.append(f'  {step["from"]}  →  [{step["external"]}]')
            elif 'ambiguous' in step:
                impls = ', '.join(step.get('implementations', []))
                lines.append(f'  {step["from"]}  →  [{step["ambiguous"]}] (ambiguous: {impls})')
            elif step.get('truncated'):
                lines.append(f'  {step["from"]}  →  … (truncated at depth {step["depth"]})')
            else:
                lines.append(f'  {step["from"]}  →  {step["to"]}()')

    # Include externals from component graph if flow didn't reach them
    flow_externals = collect_downstream_externals(flow)
    extra = [e for e in (comp_externals or []) if e not in flow_externals]
    if extra:
        lines.append(f'External systems (from graph): {", ".join(extra)}')

    return '\n'.join(lines)
