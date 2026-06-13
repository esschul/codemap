import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codemap.model import Component, Endpoint
from codemap.resolve import resolve


def _comp(name, kind='SERVICE', deps=None, externals=None, endpoints=None):
    c = Component(name=name, kind=kind, package='com.example', file=f'{name}.kt')
    c.dependencies = list(deps or [])
    c.external_systems = list(externals or [])
    c.endpoints = list(endpoints or [])
    return c


def test_resolve_strips_unknown_dependencies():
    svc = _comp('OrderService', deps=['UnknownRepo', 'KnownRepo'])
    repo = _comp('KnownRepo', kind='REPOSITORY')
    resolve([svc, repo])
    assert svc.dependencies == ['KnownRepo']


def test_resolve_impl_suffix_resolution():
    svc = _comp('OrderService', deps=['PaymentGateway'])
    impl = _comp('PaymentGatewayImpl', kind='CLIENT')
    resolve([svc, impl])
    assert 'PaymentGatewayImpl' in svc.dependencies


def test_resolve_self_reference_stripped():
    svc = _comp('OrderService', deps=['OrderService', 'Repo'])
    repo = _comp('Repo', kind='REPOSITORY')
    resolve([svc, repo])
    assert 'OrderService' not in svc.dependencies


def test_resolve_unresolved_service_becomes_external():
    svc = _comp('OrderService', deps=['InventoryService'])
    resolve([svc])
    assert svc.dependencies == []
    assert 'inventory' in svc.external_systems


def test_resolve_unresolved_client_becomes_external():
    svc = _comp('PaymentService', deps=['StripeClient'])
    resolve([svc])
    assert 'stripe' in svc.external_systems


def test_resolve_noise_types_ignored():
    svc = _comp('OrderService', deps=['String', 'ObjectMapper', 'Logger'])
    resolve([svc])
    assert svc.dependencies == []
    assert svc.external_systems == []


def test_resolve_filters_endpoint_calls():
    ep = Endpoint(http_method='GET', path='/x', handler='handle', calls=['KnownSvc', 'Ghost'])
    ctrl = _comp('Ctrl', kind='CONTROLLER', endpoints=[ep])
    known = _comp('KnownSvc')
    resolve([ctrl, known])
    assert ctrl.endpoints[0].calls == ['KnownSvc']


def test_resolve_known_http_client_types_labelled():
    svc = _comp('MyService', deps=['RestTemplate'])
    resolve([svc])
    assert 'http' in svc.external_systems


def test_resolve_deduplicates_dependencies():
    svc = _comp('OrderService', deps=['Repo', 'Repo'])
    repo = _comp('Repo', kind='REPOSITORY')
    resolve([svc, repo])
    assert svc.dependencies.count('Repo') == 1


def test_resolve_too_long_label_rejected():
    # A class with 4+ camel-case words in prefix shouldn't become an external system
    svc = _comp('OrderService', deps=['VeryLongComplexInternalHelperService'])
    resolve([svc])
    # should be dropped (too many dashes) or accepted depending on word count
    # 'very-long-complex-internal-helper' has 4 dashes → rejected
    assert 'very-long-complex-internal-helper' not in svc.external_systems
