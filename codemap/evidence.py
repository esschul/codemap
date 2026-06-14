"""
Bounded call graph evidence for an endpoint.

Traverses fieldCalls transitively across known Spring components,
stopping at REPOSITORY/CLIENT boundaries or max_depth hops.
Returns a structured flow list for use in AI prompts and debug output.
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
) -> list[dict]:
    """
    Returns a list of flow steps:
      {"from": "CtrlName.handlerMethod", "to": "ServiceName.calledMethod"}
      {"from": "ServiceName.calledMethod", "to": "RepoName.repoMethod"}
      {"from": "RepoName", "external": "database"}
    """
    flow: list[dict] = []
    seen: set[str] = set()  # "CompName.methodName" pairs already expanded

    def _expand(comp: Component, method: str, depth: int) -> None:
        key = f'{comp.name}.{method}'
        if key in seen or depth > max_depth:
            return
        seen.add(key)

        # Find the fieldCalls for this method in this component
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
                # Not a known component — skip (already handled as external at component level)
                continue

            step_key = f'{comp.name}.{method}→{target.name}.{called_method}'
            if step_key in seen:
                continue
            seen.add(step_key)

            flow.append({
                'from': f'{comp.name}.{method}',
                'to': f'{target.name}.{called_method}',
            })

            # Add external system steps from the target component
            for ext in target.external_systems:
                flow.append({'from': target.name, 'external': ext})

            # Stop recursing into repositories/clients — they ARE the boundary
            if target.kind not in _STOP_KINDS:
                _expand(target, called_method, depth + 1)

    _expand(ctrl, ep.handler, 0)
    return flow


def format_evidence_text(ep: Endpoint, ctrl: Component, flow: list[dict]) -> str:
    """Compact text representation for use in LLM prompts."""
    lines = [f'Endpoint: {ep.http_method} {ep.path}']
    lines.append(f'Handler:  {ctrl.name}.{ep.handler}()')
    if not flow:
        lines.append('Call flow: (no method-level evidence)')
        return '\n'.join(lines)
    lines.append('Call flow:')
    for step in flow:
        if 'external' in step:
            lines.append(f'  {step["from"]}  →  [{step["external"]}]')
        else:
            lines.append(f'  {step["from"]}  →  {step["to"]}()')
    return '\n'.join(lines)
