import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codemap.model import Component, Endpoint
from codemap.markdown import (
    generate_markdown, generate_endpoint_docs, _generate_docs_index, _slug, _nid,
)


def _comp(name, kind='SERVICE', domain='', deps=None, endpoints=None, externals=None):
    c = Component(name=name, kind=kind, package=f'com.example.{domain or "app"}',
                  file=f'{name}.kt', domain=domain)
    c.dependencies = list(deps or [])
    c.endpoints = list(endpoints or [])
    c.external_systems = list(externals or [])
    return c


# ── _slug ─────────────────────────────────────────────────────────────────────

def test_slug_basic():
    assert _slug('GET', '/orders/{id}') == 'get-orders-id'


def test_slug_strips_leading_trailing_dashes():
    assert not _slug('POST', '/').startswith('-')


def test_slug_lowercase():
    assert _slug('DELETE', '/Foo/Bar') == 'delete-foo-bar'


# ── _nid ──────────────────────────────────────────────────────────────────────

def test_nid_replaces_non_word():
    assert _nid('foo-bar.baz') == 'foo_bar_baz'


# ── generate_markdown ─────────────────────────────────────────────────────────

def test_generate_markdown_includes_component_names():
    svc = _comp('OrderService', domain='order')
    md = generate_markdown([svc])
    assert 'OrderService' in md


def test_generate_markdown_includes_http_endpoints():
    ctrl = _comp('OrderController', kind='CONTROLLER', domain='order',
                 endpoints=[Endpoint(http_method='GET', path='/orders', handler='list')])
    md = generate_markdown([ctrl])
    assert '/orders' in md
    assert 'GET' in md


def test_generate_markdown_includes_external_systems():
    svc = _comp('PaymentService', domain='payment', externals=['stripe'])
    md = generate_markdown([svc])
    assert 'stripe' in md


def test_generate_markdown_domain_sections():
    a = _comp('OrderService', domain='order')
    b = _comp('PaymentService', domain='payment')
    md = generate_markdown([a, b])
    assert 'Order' in md
    assert 'Payment' in md


def test_generate_markdown_mermaid_block():
    svc = _comp('OrderService', domain='order')
    md = generate_markdown([svc])
    assert '```mermaid' in md


# ── generate_endpoint_docs ────────────────────────────────────────────────────

def test_generate_endpoint_docs_creates_file_per_endpoint():
    ctrl = _comp('OrderController', kind='CONTROLLER', domain='order', endpoints=[
        Endpoint(http_method='GET', path='/orders', handler='list'),
        Endpoint(http_method='POST', path='/orders', handler='create'),
    ])
    out = Path(tempfile.mkdtemp())
    n = generate_endpoint_docs([ctrl], out)
    assert n == 2
    files = list(out.glob('*.md'))
    assert len(files) == 2


def test_generate_endpoint_docs_file_content():
    ep = Endpoint(http_method='GET', path='/orders/{id}', handler='getOrder')
    ctrl = _comp('OrderController', kind='CONTROLLER', domain='order', endpoints=[ep])
    out = Path(tempfile.mkdtemp())
    generate_endpoint_docs([ctrl], out)
    slug = _slug('GET', '/orders/{id}')
    content = (out / f'{slug}.md').read_text()
    assert 'GET /orders/{id}' in content
    assert 'getOrder' in content
    assert 'OrderController' in content


def test_generate_endpoint_docs_includes_chain():
    # _endpoint_chain follows ctrl.dependencies, so wire deps correctly
    ep = Endpoint(http_method='GET', path='/orders', handler='list', calls=['OrderService'])
    ctrl = _comp('OrderController', kind='CONTROLLER', domain='order', endpoints=[ep],
                 deps=['OrderService'])
    svc = _comp('OrderService', domain='order', externals=['database'])
    out = Path(tempfile.mkdtemp())
    generate_endpoint_docs([ctrl, svc], out)
    slug = _slug('GET', '/orders')
    content = (out / f'{slug}.md').read_text()
    assert 'OrderService' in content
    assert 'database' in content


# ── _generate_docs_index ──────────────────────────────────────────────────────

def test_generate_docs_index_creates_index_md():
    ctrl = _comp('OrderController', kind='CONTROLLER', domain='order', endpoints=[
        Endpoint(http_method='GET', path='/orders', handler='list'),
    ])
    out = Path(tempfile.mkdtemp())
    _generate_docs_index(out, 'My App', [ctrl])
    index = (out / 'index.md').read_text()
    assert 'My App' in index
    assert 'OrderController' in index
    assert 'GET' in index


def test_generate_docs_index_summary_counts():
    ctrl = _comp('Ctrl', kind='CONTROLLER', endpoints=[
        Endpoint(http_method='GET', path='/a', handler='a'),
        Endpoint(http_method='POST', path='/b', handler='b'),
    ])
    svc = _comp('Svc', externals=['kafka'])
    out = Path(tempfile.mkdtemp())
    _generate_docs_index(out, 'T', [ctrl, svc])
    index = (out / 'index.md').read_text()
    assert '2 HTTP endpoints' in index
    assert 'kafka' in index
