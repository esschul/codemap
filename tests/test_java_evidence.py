"""
Golden tests for Java scanner fieldCalls + bounded evidence graph.

Covers: controller → service → interface repo → repository → database
"""
import pytest
from pathlib import Path
from codemap.scan import scan
from codemap.resolve import resolve
from codemap.evidence import build_evidence, format_evidence_text

FIXTURE = Path(__file__).parent / 'fixture-java' / 'src' / 'main'


@pytest.fixture(scope='module')
def java_world():
    comps, _, _ = scan(FIXTURE)
    iface_map = resolve(comps)
    by_name = {c.name: c for c in comps}
    return comps, by_name, iface_map


def _ep(by_name, ctrl_name, handler_name):
    ctrl = by_name[ctrl_name]
    return next(ep for ep in ctrl.endpoints if ep.handler == handler_name)


# ── Java AST scanner: fieldCalls format ──────────────────────────────────────

def test_java_field_calls_format(java_world):
    """fieldCalls has {field, type, method} with interface type preserved."""
    _, by_name, _ = java_world
    ep = _ep(by_name, 'OrderController', 'getOrder')
    # OrderController.getOrder calls orderService.findOrder()
    assert any(
        fc['field'] == 'orderService' and fc['method'] == 'findOrder'
        for fc in ep.field_calls
    ), f"expected orderService.findOrder in {ep.field_calls}"


def test_java_calls_on_fields_backward_compat(java_world):
    """ep.calls still contains service class names (backward compat)."""
    _, by_name, _ = java_world
    ep = _ep(by_name, 'OrderController', 'getOrder')
    assert 'OrderService' in ep.calls


def test_java_private_helper_expanded(java_world):
    """createOrder follows private validate() helper → sees existsById."""
    _, by_name, _ = java_world
    svc = by_name['OrderService']
    method_calls = svc.method_field_calls.get('createOrder', [])
    method_names = [fc['method'] for fc in method_calls]
    assert 'save' in method_names, f"save missing from createOrder: {method_names}"
    assert 'existsById' in method_names, f"existsById (via private helper) missing: {method_names}"


def test_java_public_sibling_not_expanded(java_world):
    """getOrder does NOT pick up createOrder's private helper calls."""
    _, by_name, _ = java_world
    svc = by_name['OrderService']
    method_calls = svc.method_field_calls.get('findOrder', [])
    method_names = [fc['method'] for fc in method_calls]
    assert 'existsById' not in method_names, f"existsById should not appear in findOrder: {method_names}"
    assert 'save' not in method_names


# ── Interface resolution ──────────────────────────────────────────────────────

def test_java_interface_resolved(java_world):
    """OrderService.dependencies resolves OrderRepository via OrderStore implements."""
    _, by_name, _ = java_world
    svc = by_name['OrderService']
    assert 'OrderRepository' in svc.dependencies, f"deps: {svc.dependencies}"


def test_java_repository_implements_captured(java_world):
    """OrderRepository.implements contains OrderStore."""
    _, by_name, _ = java_world
    repo = by_name['OrderRepository']
    assert 'OrderStore' in repo.implements


# ── Bounded evidence graph ────────────────────────────────────────────────────

def test_evidence_get_order_two_levels(java_world):
    """GET /orders/{id}: controller → service.findOrder → repo.findById → [database]."""
    _, by_name, iface_map = java_world
    ctrl = by_name['OrderController']
    ep = _ep(by_name, 'OrderController', 'getOrder')
    flow = build_evidence(ep, ctrl, by_name, iface_map=iface_map)

    froms = [s['from'] for s in flow]
    tos = [s.get('to', '') for s in flow]
    externals = [s.get('external', '') for s in flow]

    assert any('OrderController.getOrder' in f for f in froms), f"flow: {flow}"
    assert any('OrderService.findOrder' in t for t in tos), f"flow: {flow}"
    assert any('OrderRepository.findById' in t for t in tos), f"flow: {flow}"


def test_evidence_create_order_includes_helper_calls(java_world):
    """POST /orders: flow includes existsById from private helper."""
    _, by_name, iface_map = java_world
    ctrl = by_name['OrderController']
    ep = _ep(by_name, 'OrderController', 'createOrder')
    flow = build_evidence(ep, ctrl, by_name, iface_map=iface_map)

    tos = [s.get('to', '') for s in flow]
    assert any('existsById' in t for t in tos), \
        f"existsById (from private helper) missing from flow: {flow}"
    assert any('save' in t for t in tos), f"save missing from flow: {flow}"


def test_evidence_stops_at_repository(java_world):
    """Flow does not recurse beyond REPOSITORY kind."""
    _, by_name, iface_map = java_world
    ctrl = by_name['OrderController']
    ep = _ep(by_name, 'OrderController', 'getOrder')
    flow = build_evidence(ep, ctrl, by_name, iface_map=iface_map)

    # No step should originate FROM OrderRepository (it's a stop boundary)
    repo_as_from = [s for s in flow if s.get('from', '').startswith('OrderRepository.')]
    assert not repo_as_from, f"Should not recurse into repo internals: {repo_as_from}"


def test_evidence_text_format(java_world):
    """format_evidence_text produces readable output."""
    _, by_name, iface_map = java_world
    ctrl = by_name['OrderController']
    ep = _ep(by_name, 'OrderController', 'getOrder')
    flow = build_evidence(ep, ctrl, by_name, iface_map=iface_map)
    text = format_evidence_text(ep, ctrl, flow)

    assert 'GET' in text
    assert 'OrderController.getOrder' in text
    assert 'OrderService.findOrder' in text
