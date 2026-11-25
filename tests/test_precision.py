"""
Regression tests for call-chain precision:
- external systems are attributed per method, not per class
- scheduled jobs / listeners count as entrypoints for reachability
- types mapped to an external (or Kotlin built-ins) are not reported as dropped
- @Service classes with @KafkaListener methods are consumers
"""
import tempfile
from pathlib import Path

import pytest

from codemap.evidence import build_evidence, method_externals
from codemap.model import Component, Endpoint, NonHttpEntrypoint
from codemap.resolve import resolve, resolve_full, ResolveDiagnostics
from codemap.scan import parse_file, scan

FIXTURE = Path(__file__).parent / 'fixture-spring' / 'src' / 'main'


@pytest.fixture(scope='module')
def world():
    comps, _, _ = scan(FIXTURE)
    iface_map, ambiguous = resolve_full(comps)
    return {c.name: c for c in comps}, iface_map, ambiguous


def _flow(world, ctrl_name, handler):
    by_name, iface_map, ambiguous = world
    ctrl = by_name[ctrl_name]
    ep = next(e for e in ctrl.endpoints if e.handler == handler)
    return build_evidence(ep, ctrl, by_name, iface_map=iface_map, ambiguous_ifaces=ambiguous)


def _externals_of(flow, comp_name):
    return {s['external'] for s in flow if s.get('from') == comp_name and 'external' in s}


def test_read_endpoint_does_not_reach_kafka(world):
    # OrderService has a KafkaTemplate, but findById() never calls it
    flow = _flow(world, 'OrderController', 'getOrder')
    assert 'kafka' not in _externals_of(flow, 'OrderService')
    assert 'database' in _externals_of(flow, 'OrderRepository')


def test_write_endpoint_still_reaches_kafka(world):
    flow = _flow(world, 'OrderController', 'placeOrder')
    assert 'kafka' in _externals_of(flow, 'OrderService')


def test_class_level_externals_apply_to_every_method(world):
    by_name = world[0]
    # Feign client: the external comes from @FeignClient, not from a field
    assert method_externals(by_name['InventoryClient'], 'reserve') == ['inventory-service']


def test_method_without_evidence_falls_back_to_all_externals():
    comp = Component('Repo', 'REPOSITORY', 'p', 'f', external_systems=['database'])
    assert method_externals(comp, 'save') == ['database']


def test_scheduled_job_dependencies_are_reachable():
    job = Component('NightlyExporter', 'COMPONENT', 'p', 'f', dependencies=['Uploader'],
                    non_http_entrypoints=[NonHttpEntrypoint('SCHEDULED', 'run', '')])
    uploader = Component('Uploader', 'SERVICE', 'p', 'f')
    orphan = Component('Orphan', 'SERVICE', 'p', 'f')
    diag = ResolveDiagnostics()
    resolve([job, uploader, orphan], diagnostics=diag)
    assert diag.unreachable == ['Orphan']


def test_known_external_and_kotlin_builtin_types_are_not_dropped():
    comp = Component('DbCache', 'SERVICE', 'p', 'f',
                     dependencies=['NamedParameterJdbcTemplate', 'Int'],
                     external_systems=['database'])
    diag = ResolveDiagnostics()
    resolve([comp], diagnostics=diag)
    assert diag.dropped == []


def test_service_with_kafka_listener_is_consumer(world):
    consumer = world[0]['NotificationConsumer']
    assert consumer.kind == 'CONSUMER'
    assert 'kafka' in consumer.external_systems
    assert consumer.non_http_entrypoints[0].detail == 'orders.placed'


def test_scheduled_with_stacked_annotation_and_constant():
    tmp = Path(tempfile.mkdtemp()) / 'Exporter.kt'
    tmp.write_text("""
package com.example.export

import org.springframework.scheduling.annotation.Scheduled
import org.springframework.stereotype.Component

@Component
class Exporter(private val uploader: Uploader) {
    @Scheduled(cron = EVERY_NIGHT)
    @SchedulerLock(name = "export")
    fun export() { uploader.upload() }
}
""", encoding='utf-8')
    comp = parse_file(tmp)
    assert comp is not None
    [ep] = comp.non_http_entrypoints
    assert (ep.kind, ep.method, ep.detail) == ('SCHEDULED', 'export', 'cron=EVERY_NIGHT')
