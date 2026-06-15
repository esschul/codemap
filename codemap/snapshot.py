"""Snapshot and diff utilities for live architecture view."""

from __future__ import annotations
from dataclasses import dataclass, field as dc_field
from typing import Any


# ---------------------------------------------------------------------------
# Snapshot serialisation
# ---------------------------------------------------------------------------

def to_snapshot(components: list) -> dict:
    """Serialise a list of Component objects to a JSON-serialisable dict."""
    out: dict[str, Any] = {}
    for c in components:
        endpoints = []
        for ep in c.endpoints:
            endpoints.append({
                'method': ep.http_method,
                'path': ep.path,
                'handler': ep.handler,
            })
        out[c.name] = {
            'kind': c.kind,
            'file': c.file,
            'domain': c.domain or '',
            'dependencies': list(c.dependencies),
            'endpoints': endpoints,
            'externals': list(c.external_systems),
            # method → [{field, type, method}] for call-chain diffing
            'callChains': {
                handler: calls
                for handler, calls in (c.method_field_calls or {}).items()
            },
        }
    return out


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def diff_snapshots(prev: dict, curr: dict) -> dict:
    """
    Compute the structural diff between two snapshots.

    Returns a dict with:
      addedComponents, removedComponents,
      changedComponents (per-component detail),
      addedEdges, removedEdges,
      sessionChangedFiles (always empty here — caller can populate)
    """
    prev_names = set(prev)
    curr_names = set(curr)

    added = sorted(curr_names - prev_names)
    removed = sorted(prev_names - curr_names)

    changed: dict[str, Any] = {}
    added_edges: list[list[str]] = []
    removed_edges: list[list[str]] = []

    for name in curr_names & prev_names:
        p = prev[name]
        c = curr[name]

        # Endpoints
        prev_eps = {f"{ep['method']} {ep['path']}" for ep in p.get('endpoints', [])}
        curr_eps = {f"{ep['method']} {ep['path']}" for ep in c.get('endpoints', [])}
        added_eps = sorted(curr_eps - prev_eps)
        removed_eps = sorted(prev_eps - curr_eps)

        # Dependencies (edges)
        prev_deps = set(p.get('dependencies', []))
        curr_deps = set(c.get('dependencies', []))
        for dep in curr_deps - prev_deps:
            added_edges.append([name, dep])
        for dep in prev_deps - curr_deps:
            removed_edges.append([name, dep])
        added_deps = sorted(curr_deps - prev_deps)
        removed_deps = sorted(prev_deps - curr_deps)

        # Call chains (per handler)
        prev_chains = p.get('callChains', {})
        curr_chains = c.get('callChains', {})
        chain_changes: dict[str, Any] = {}

        all_handlers = set(prev_chains) | set(curr_chains)
        for handler in all_handlers:
            pc = prev_chains.get(handler, [])
            cc = curr_chains.get(handler, [])
            # Compare as sets of (type, method) tuples
            pc_set = {(x.get('type', ''), x.get('method', '')) for x in pc}
            cc_set = {(x.get('type', ''), x.get('method', '')) for x in cc}
            added_calls = [{'type': t, 'method': m} for t, m in sorted(cc_set - pc_set)]
            removed_calls = [{'type': t, 'method': m} for t, m in sorted(pc_set - cc_set)]
            if added_calls or removed_calls:
                chain_changes[handler] = {
                    'addedCalls': added_calls,
                    'removedCalls': removed_calls,
                }

        if added_eps or removed_eps or added_deps or removed_deps or chain_changes:
            changed[name] = {
                'addedEndpoints': added_eps,
                'removedEndpoints': removed_eps,
                'addedDependencies': added_deps,
                'removedDependencies': removed_deps,
                'changedCallChains': chain_changes,
            }

    # Also emit edges for newly added/removed components
    for name in added:
        for dep in curr[name].get('dependencies', []):
            added_edges.append([name, dep])
    for name in removed:
        for dep in prev[name].get('dependencies', []):
            removed_edges.append([name, dep])

    return {
        'addedComponents': added,
        'removedComponents': removed,
        'changedComponents': changed,
        'addedEdges': added_edges,
        'removedEdges': removed_edges,
        'sessionChangedFiles': [],
    }


def is_empty_diff(d: dict) -> bool:
    return (
        not d['addedComponents']
        and not d['removedComponents']
        and not d['changedComponents']
        and not d['addedEdges']
        and not d['removedEdges']
    )


# ---------------------------------------------------------------------------
# Terminal formatting for --debug-diff
# ---------------------------------------------------------------------------

def format_diff(d: dict) -> str:
    lines: list[str] = []

    for name in d['addedComponents']:
        lines.append(f'  + component {name}')
    for name in d['removedComponents']:
        lines.append(f'  - component {name}')

    for name, ch in d['changedComponents'].items():
        for ep in ch['addedEndpoints']:
            lines.append(f'  + endpoint {ep}  ({name})')
        for ep in ch['removedEndpoints']:
            lines.append(f'  - endpoint {ep}  ({name})')
        for dep in ch['addedDependencies']:
            lines.append(f'  ~ {name}: added dependency {dep}')
        for dep in ch['removedDependencies']:
            lines.append(f'  ~ {name}: removed dependency {dep}')
        for handler, cc in ch['changedCallChains'].items():
            for call in cc['addedCalls']:
                lines.append(f'  ~ {name}.{handler}(): added call → {call["type"]}.{call["method"]}()')
            for call in cc['removedCalls']:
                lines.append(f'  ~ {name}.{handler}(): removed call → {call["type"]}.{call["method"]}()')

    for src, dst in d['addedEdges']:
        if src not in d['addedComponents']:  # already implied by component add
            lines.append(f'  + edge {src} → {dst}')
    for src, dst in d['removedEdges']:
        if src not in d['removedComponents']:
            lines.append(f'  - edge {src} → {dst}')

    return '\n'.join(lines) if lines else '  (no structural changes)'
