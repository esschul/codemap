import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codemap.scan import (
    parse_file, _infer_kind, _infer_domain, _infer_externals, ROLE_ANNOTATIONS,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _write_kt(source: str, filename: str = 'Demo.kt') -> Path:
    tmp = Path(tempfile.mkdtemp())
    p = tmp / filename
    p.write_text(source, encoding='utf-8')
    return p


def _write_java(source: str, filename: str = 'Demo.java') -> Path:
    tmp = Path(tempfile.mkdtemp())
    p = tmp / filename
    p.write_text(source, encoding='utf-8')
    return p


# ── _infer_kind ───────────────────────────────────────────────────────────────

def test_infer_kind_annotation_priority_over_suffix():
    kind, reason = _infer_kind('OrderService', ['FeignClient'])
    assert kind == 'CLIENT'
    assert '@FeignClient' in reason


def test_infer_kind_suffix_when_no_annotation():
    kind, reason = _infer_kind('OrderRepository', [])
    assert kind == 'REPOSITORY'
    assert 'Repository' in reason


def test_infer_kind_body_annotation_secondary():
    # Name must not match any suffix (EventHandler → COMPONENT via suffix, so use a neutral name)
    kind, reason = _infer_kind('MessageProcessor', [], body_anns=['EventListener'])
    assert kind == 'LISTENER'
    assert 'secondary' in reason


def test_infer_kind_fallback():
    kind, _ = _infer_kind('SomeRandomClass', [])
    assert kind == 'COMPONENT'


def test_infer_kind_client_suffixes():
    for suffix in ('Gateway', 'Invoker', 'Adapter'):
        kind, _ = _infer_kind(f'Payment{suffix}', [])
        assert kind == 'CLIENT', f'Expected CLIENT for suffix {suffix}'


# ── _infer_domain ─────────────────────────────────────────────────────────────

def test_infer_domain_strips_noise():
    assert _infer_domain('com.example.order.service') == 'order'


def test_infer_domain_returns_last_meaningful_segment():
    assert _infer_domain('no.posten.shipping.consignment') == 'consignment'


def test_infer_domain_all_noise_returns_empty():
    # All segments in noise set → empty string
    assert _infer_domain('com.org.net.service.api') == ''


# ── _infer_externals ──────────────────────────────────────────────────────────

def test_infer_externals_kafka():
    result = _infer_externals(['KafkaTemplate'])
    assert 'kafka' in result


def test_infer_externals_database():
    result = _infer_externals(['JpaRepository'])
    assert 'database' in result


def test_infer_externals_multiple():
    result = _infer_externals(['KafkaTemplate', 'S3Client'])
    assert 'kafka' in result
    assert 's3' in result


def test_infer_externals_unknown_type():
    result = _infer_externals(['SomeUnknownType'])
    assert result == []


# ── parse_file: Kotlin ────────────────────────────────────────────────────────

def test_parse_kotlin_service():
    p = _write_kt("""
    package com.example.order

    import org.springframework.stereotype.Service

    @Service
    class OrderService(private val repo: OrderRepository)
    """)
    comp = parse_file(p)
    assert comp is not None
    assert comp.kind == 'SERVICE'
    assert comp.name == 'OrderService'
    assert 'OrderRepository' in comp.dependencies


def test_parse_kotlin_controller_with_base_path():
    p = _write_kt("""
    package com.example.order

    import org.springframework.web.bind.annotation.*

    @RestController
    @RequestMapping("/orders")
    class OrderController(private val svc: OrderService) {
        @GetMapping("/{id}")
        fun getOrder() = svc.find()

        @PostMapping
        fun createOrder() = svc.create()
    }
    """)
    comp = parse_file(p)
    assert comp is not None
    assert comp.kind == 'CONTROLLER'
    assert comp.base_path == '/orders'
    paths = {e.path for e in comp.endpoints}
    assert '/orders/{id}' in paths
    assert '/orders' in paths


def test_parse_kotlin_feign_client_adds_external():
    p = _write_kt("""
    package com.example.payment

    import org.springframework.cloud.openfeign.FeignClient

    @FeignClient(name = "stripe-service")
    interface StripeClient {
        fun charge(): String
    }
    """, filename='StripeClient.kt')
    comp = parse_file(p)
    assert comp is not None
    assert comp.kind == 'CLIENT'
    assert 'stripe-service' in comp.external_systems


def test_parse_kotlin_repository():
    p = _write_kt("""
    package com.example.order

    import org.springframework.stereotype.Repository

    @Repository
    class OrderRepository
    """)
    comp = parse_file(p)
    assert comp is not None
    assert comp.kind == 'REPOSITORY'


def test_parse_kotlin_skips_springbootapplication():
    p = _write_kt("""
    package com.example

    import org.springframework.boot.autoconfigure.SpringBootApplication

    @SpringBootApplication
    class Application
    """)
    assert parse_file(p) is None


def test_parse_kotlin_skips_test_files():
    p = _write_kt("""
    package com.example

    import org.springframework.stereotype.Service

    @Service
    class OrderService
    """, filename='OrderServiceTest.kt')
    assert parse_file(p) is None


def test_parse_kotlin_non_http_entrypoints_scheduled():
    p = _write_kt("""
    package com.example.job

    import org.springframework.scheduling.annotation.Scheduled
    import org.springframework.stereotype.Component

    @Component
    class CleanupJob {
        @Scheduled(cron = "0 0 * * * *")
        fun cleanup() {}
    }
    """)
    comp = parse_file(p)
    assert comp is not None
    nheps = comp.non_http_entrypoints
    assert len(nheps) == 1
    assert nheps[0].kind == 'SCHEDULED'
    assert nheps[0].method == 'cleanup'


def test_parse_kotlin_kafka_listener():
    p = _write_kt("""
    package com.example.event

    import org.springframework.kafka.annotation.KafkaListener
    import org.springframework.stereotype.Service

    @Service
    class OrderConsumer {
        @KafkaListener(topics = "orders")
        fun consume(msg: String) {}
    }
    """)
    comp = parse_file(p)
    assert comp is not None
    nheps = comp.non_http_entrypoints
    assert any(n.kind == 'KAFKA' and n.detail == 'orders' for n in nheps)


def test_parse_kotlin_loc_counted():
    src = 'package com.example\n\nimport org.springframework.stereotype.Service\n\n@Service\nclass Foo\n'
    p = _write_kt(src)
    comp = parse_file(p)
    assert comp is not None
    assert comp.loc == src.count('\n') + 1


def test_parse_kotlin_endpoint_calls_follow_private_helper():
    # Expression bodies (fun f() = expr) have no braces, so use a block body
    p = _write_kt("""
    package com.example

    import org.springframework.web.bind.annotation.*

    @RestController
    class Ctrl(private val svc: MyService) {
        @GetMapping("/go")
        fun handle(): String {
            return doWork()
        }

        private fun doWork(): String {
            return svc.run()
        }
    }
    """)
    comp = parse_file(p)
    assert comp is not None
    ep = comp.endpoints[0]
    assert 'MyService' in ep.calls


def test_parse_kotlin_domain_inferred_from_package():
    p = _write_kt("""
    package no.example.shipping.consignment

    import org.springframework.stereotype.Service

    @Service
    class ShipmentService
    """)
    comp = parse_file(p)
    assert comp is not None
    assert comp.domain == 'consignment'


# ── parse_file: Java ──────────────────────────────────────────────────────────

def test_parse_java_service_constructor_injection():
    p = _write_java("""
    package com.example.order;

    import org.springframework.stereotype.Service;

    @Service
    public class OrderService {
        private final OrderRepository repo;

        public OrderService(OrderRepository repo) {
            this.repo = repo;
        }
    }
    """, filename='OrderService.java')
    comp = parse_file(p)
    assert comp is not None
    assert comp.kind == 'SERVICE'
    assert 'OrderRepository' in comp.dependencies


def test_parse_java_skips_domain_path_without_annotation():
    p = _write_java("""
    package com.example.domain;

    public class Order {
        private String id;
    }
    """, filename='Order.java')
    # domain/ path without Spring annotation should be skipped
    assert parse_file(p) is None


def test_parse_java_allows_domain_path_with_annotation():
    p = _write_java("""
    package com.example.domain;

    import org.springframework.stereotype.Repository;

    @Repository
    public class OrderRepository {}
    """, filename='OrderRepository.java')
    comp = parse_file(p)
    assert comp is not None
    assert comp.kind == 'REPOSITORY'
