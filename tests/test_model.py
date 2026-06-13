import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codemap.model import Component, Endpoint, NonHttpEntrypoint


def test_component_defaults():
    c = Component(name='Foo', kind='SERVICE', package='com.example', file='Foo.kt')
    assert c.endpoints == []
    assert c.non_http_entrypoints == []
    assert c.dependencies == []
    assert c.field_map == {}
    assert c.external_systems == []
    assert c.spring_annotations == []
    assert c.classification_reason == ''
    assert c.domain == ''
    assert c.capability == ''
    assert c.loc == 0
    assert c.base_path == ''


def test_endpoint_defaults():
    e = Endpoint(http_method='GET', path='/foo', handler='getFoo')
    assert e.calls == []


def test_non_http_entrypoint_fields():
    n = NonHttpEntrypoint(kind='KAFKA', method='consume', detail='orders-topic')
    assert n.kind == 'KAFKA'
    assert n.method == 'consume'
    assert n.detail == 'orders-topic'


def test_component_mutable_lists_are_independent():
    a = Component(name='A', kind='SERVICE', package='com.example', file='A.kt')
    b = Component(name='B', kind='SERVICE', package='com.example', file='B.kt')
    a.dependencies.append('Foo')
    assert b.dependencies == []
