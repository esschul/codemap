"""
Comparison tests: Kotlin AST scanner vs regex scanner.

Each test checks that the AST scanner's output agrees with the regex scanner on a
specific aspect (class name, injected fields, endpoint paths, call detection).
Where the two disagree, the test documents which is correct and why.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from codemap.scan import _KT_JAR, _kt_scan_files, parse_file

FIXTURES = Path(__file__).parent / "fixture-spring" / "src" / "main" / "kotlin"

# ── helpers ───────────────────────────────────────────────────────────────────


def ast(path: Path) -> dict | None:
    """Run the AST scanner on a single file, return its result dict or None."""
    results = _kt_scan_files([path])
    return results.get(str(path))


def regex(path: Path):
    """Run the regex scanner on a single file, return the Component or None."""
    return parse_file(path)


def kt(rel: str) -> Path:
    return FIXTURES / rel


# ── skip if JAR not available ─────────────────────────────────────────────────

pytestmark = pytest.mark.skipif(
    not _KT_JAR.exists(),
    reason="kt-scanner.jar not found — skipping AST comparison tests",
)


# ── class name ────────────────────────────────────────────────────────────────


def test_class_name_matches_regex():
    path = kt("com/example/order/OrderController.kt")
    a = ast(path)
    r = regex(path)
    assert a is not None and r is not None
    assert a["className"] == r.name


def test_class_name_service():
    path = kt("com/example/order/OrderService.kt")
    a = ast(path)
    assert a["className"] == "OrderService"


# ── injected fields ───────────────────────────────────────────────────────────


def test_controller_single_dep_matches():
    """OrderController has one primary-constructor dep: OrderService."""
    path = kt("com/example/order/OrderController.kt")
    a = ast(path)
    r = regex(path)
    ast_types = {f["type"] for f in a["fields"]}
    regex_types = set(r.dependencies)
    assert ast_types == regex_types == {"OrderService"}


def test_service_three_deps_matches():
    """OrderService: OrderRepository, InventoryClient, KafkaTemplate."""
    path = kt("com/example/order/OrderService.kt")
    a = ast(path)
    r = regex(path)
    ast_types = {f["type"] for f in a["fields"]}
    regex_types = set(r.dependencies)
    assert ast_types == regex_types == {"OrderRepository", "InventoryClient", "KafkaTemplate"}


def test_kafka_generic_type_stripped():
    """KafkaTemplate<String, Any> → type name must be KafkaTemplate, not Any or String."""
    path = kt("com/example/order/OrderService.kt")
    a = ast(path)
    ast_types = {f["type"] for f in a["fields"]}
    assert "KafkaTemplate" in ast_types
    assert "String" not in ast_types
    assert "Any" not in ast_types


def test_qualified_type_simplified():
    """com.example.order.OrderService as a type → AST must return 'OrderService'."""
    path = kt("com/example/payment/PaymentService.kt")
    a = ast(path)
    r = regex(path)
    ast_types = {f["type"] for f in a["fields"]}
    regex_types = set(r.dependencies)
    assert "OrderService" in ast_types
    assert ast_types == regex_types


def test_consumer_deps_match():
    path = kt("com/example/notification/NotificationConsumer.kt")
    a = ast(path)
    r = regex(path)
    ast_types = {f["type"] for f in a["fields"]}
    assert ast_types == set(r.dependencies)


def test_interface_has_no_fields():
    """InventoryClient is an interface — no injectable fields expected from either scanner."""
    path = kt("com/example/order/InventoryClient.kt")
    a = ast(path)
    r = regex(path)
    # AST returns None for interfaces (no class body with constructor)
    # Regex may return a component with empty deps
    if a is not None:
        assert {f["type"] for f in a["fields"]} == set()
    if r is not None:
        assert r.dependencies == []


# ── AST catches method-param false positives that regex misses ────────────────


def test_no_fields_when_only_method_params():
    """
    PaymentValidator has no constructor and no @Autowired fields.
    The regex scanner incorrectly infers ChargeRequest (a method param) as a dep.
    The AST is correct: no injected fields.
    """
    path = kt("com/example/payment/PaymentValidator.kt")
    a = ast(path)
    r = regex(path)
    if a is not None:
        ast_types = {f["type"] for f in a["fields"]}
        assert ast_types == set(), "AST must not pick up method params as fields"
    # Regex known false positive: documents the gap, does not fail the test
    # assert set(r.dependencies) == set()  ← regex wrongly returns {'ChargeRequest'}


def test_mapper_no_injected_fields():
    """PaymentMapper: stateless mapper, method params only. AST correctly returns no fields."""
    path = kt("com/example/payment/PaymentMapper.kt")
    a = ast(path)
    if a is not None:
        assert {f["type"] for f in a["fields"]} == set()


# ── annotations ───────────────────────────────────────────────────────────────


def test_class_annotations_detected():
    path = kt("com/example/order/OrderService.kt")
    a = ast(path)
    assert "Service" in a["annotations"]


def test_controller_annotations():
    path = kt("com/example/order/OrderController.kt")
    a = ast(path)
    anns = set(a["annotations"])
    assert "RestController" in anns
    assert "RequestMapping" in anns


def test_component_annotation():
    path = kt("com/example/notification/NotificationConsumer.kt")
    a = ast(path)
    assert a is not None
    assert any(ann in a["annotations"] for ann in ("Component", "Service", "KafkaListener"))


# ── endpoint paths ────────────────────────────────────────────────────────────


def test_controller_method_count_matches_regex():
    """Both scanners should find the same number of handler methods."""
    path = kt("com/example/order/OrderController.kt")
    a = ast(path)
    r = regex(path)
    assert a is not None and r is not None
    assert len(a["methods"]) == len(r.endpoints)


def test_get_mapping_path_extracted():
    path = kt("com/example/order/OrderController.kt")
    a = ast(path)
    methods = {m["name"]: m for m in a["methods"]}
    assert methods["getOrder"]["mappingPath"] == "/{id}"
    assert methods["cancelOrder"]["mappingPath"] == "/{id}"


def test_post_mapping_no_path():
    """@PostMapping with no path argument → mappingPath is null."""
    path = kt("com/example/order/OrderController.kt")
    a = ast(path)
    methods = {m["name"]: m for m in a["methods"]}
    assert methods["placeOrder"]["mappingPath"] is None


# ── field call detection ───────────────────────────────────────────────────────


def test_service_method_calls_detected():
    """placeOrder calls all three injected fields."""
    path = kt("com/example/order/OrderService.kt")
    a = ast(path)
    methods = {m["name"]: m for m in a["methods"]}
    place = methods["placeOrder"]["callsOnFields"]
    assert "orderRepository" in place
    assert "inventoryClient" in place
    assert "kafkaTemplate" in place


def test_method_only_calls_subset():
    """refund only calls orderRepository and stripeClient, not kafkaTemplate."""
    path = kt("com/example/payment/PaymentService.kt")
    a = ast(path)
    methods = {m["name"]: m for m in a["methods"]}
    refund_calls = set(methods["refund"]["callsOnFields"])
    assert "paymentRepository" in refund_calls
    assert "stripeClient" in refund_calls
    assert "kafkaTemplate" not in refund_calls


def test_expression_body_call_detected():
    """findById uses expression body syntax: fun f() = repo.findById(...). Must detect repo call."""
    path = kt("com/example/order/OrderService.kt")
    a = ast(path)
    methods = {m["name"]: m for m in a["methods"]}
    assert "orderRepository" in methods["findById"]["callsOnFields"]


# ── regression: private-helper delegation (P1) ────────────────────────────────


def test_private_helper_delegation_followed_transitively(tmp_path):
    """
    getOrder delegates to buildResponse which calls orderService.
    The AST scanner must follow private helpers so getOrder shows orderService.
    """
    src = tmp_path / "DelegatingController.kt"
    src.write_text(
        "package demo\n"
        "import org.springframework.web.bind.annotation.*\n"
        "@RestController\n"
        "class DelegatingController(private val orderService: OrderService) {\n"
        "    @GetMapping(\"/orders/{id}\")\n"
        "    fun getOrder(id: String) = buildResponse(id)\n"
        "    private fun buildResponse(id: String) = orderService.findById(id)\n"
        "}\n"
    )
    a = ast(src)
    assert a is not None
    methods = {m["name"]: m for m in a["methods"]}
    # Transitive expansion: getOrder → buildResponse → orderService
    assert "orderService" in methods["getOrder"]["callsOnFields"]
    assert "orderService" in methods["buildResponse"]["callsOnFields"]


def test_public_sibling_not_followed_transitively(tmp_path):
    """
    getOrder calls publicHelper (a public method). publicHelper calls the repo.
    The scanner must NOT follow public siblings transitively — only private ones.
    """
    src = tmp_path / "PublicSiblingTest.kt"
    src.write_text(
        "package demo\n"
        "import org.springframework.web.bind.annotation.*\n"
        "@RestController\n"
        "class PublicSiblingTest(private val repo: OrderRepository) {\n"
        "    @GetMapping(\"/orders/{id}\")\n"
        "    fun getOrder(id: String) = publicHelper(id)\n"
        "    fun publicHelper(id: String) = repo.findById(id)\n"
        "}\n"
    )
    a = ast(src)
    assert a is not None
    methods = {m["name"]: m for m in a["methods"]}
    # getOrder only delegates to a PUBLIC method — must not get repo in its callsOnFields
    assert "repo" not in methods["getOrder"]["callsOnFields"]
    # publicHelper itself does call repo directly
    assert "repo" in methods["publicHelper"]["callsOnFields"]


def test_apply_ast_preserves_calls_when_field_map_empty(tmp_path):
    """
    If AST returns no fields (unrecognised constructor pattern), ep.calls must
    still be populated using comp.field_map (set by regex) as fallback.
    Without the effective_field_map fix, ep.calls would be emptied.
    """
    from codemap.scan import _apply_ast_result
    from codemap.model import Component, Endpoint

    ep = Endpoint(http_method="GET", path="/orders/{id}", handler="getOrder", calls=[])
    comp = Component(
        name="UnknownCtorController", kind="CONTROLLER",
        package="demo", file="demo/UnknownCtorController.kt",
    )
    comp.endpoints = [ep]
    # Regex already populated field_map and dependencies
    comp.field_map = {"orderService": "OrderService"}
    comp.dependencies = ["OrderService"]

    # AST recognises the method body calls but NOT the constructor (fields=[])
    ast_result = {
        "className": "UnknownCtorController",
        "annotations": ["RestController"],
        "fields": [],  # empty — constructor pattern unrecognised
        "methods": [
            {"name": "getOrder", "annotations": ["GetMapping"],
             "mappingPath": "/orders/{id}", "callsOnFields": ["orderService"]},
        ],
    }

    _apply_ast_result(comp, ast_result, "demo", preserve_on_empty=True)
    # dependencies must be preserved from regex (field_map was empty from AST)
    assert comp.dependencies == ["OrderService"]
    # ep.calls must be populated via effective_field_map (comp.field_map fallback)
    assert comp.endpoints[0].calls == ["OrderService"]


def test_apply_ast_clears_regex_deps_for_java(tmp_path):
    """
    Java AST (preserve_on_empty=False): when AST returns fields=[], it must
    overwrite regex-derived dependencies — this is how JavaParser removes false positives.
    """
    from codemap.scan import _apply_ast_result
    from codemap.model import Component, Endpoint

    ep = Endpoint(http_method="GET", path="/status", handler="status", calls=["FalsePositive"])
    comp = Component(
        name="StatusController", kind="CONTROLLER",
        package="demo", file="demo/StatusController.java",
    )
    comp.endpoints = [ep]
    comp.field_map = {"falsePositive": "FalsePositive"}
    comp.dependencies = ["FalsePositive"]

    ast_result = {
        "className": "StatusController",
        "annotations": ["RestController"],
        "fields": [],  # Java AST confirms: no injected fields
        "methods": [
            {"name": "status", "annotations": ["GetMapping"],
             "mappingPath": "/status", "callsOnFields": []},
        ],
    }

    _apply_ast_result(comp, ast_result, "demo", preserve_on_empty=False)
    assert comp.dependencies == [], "Java AST must clear regex false-positive dependencies"
    assert comp.endpoints[0].calls == []


def test_apply_ast_replaces_calls_unconditionally(tmp_path):
    """
    _apply_ast_result replaces ep.calls with AST result unconditionally.
    Private helpers are now followed in the scanner itself.
    """
    from codemap.scan import _apply_ast_result
    from codemap.model import Component, Endpoint

    ep = Endpoint(http_method="GET", path="/orders/{id}", handler="getOrder",
                  calls=["StaleRegexCall"])
    comp = Component(
        name="DelegatingController", kind="CONTROLLER",
        package="demo", file="demo/DelegatingController.kt",
    )
    comp.endpoints = [ep]
    comp.field_map = {"orderService": "OrderService"}
    comp.dependencies = ["OrderService"]

    ast_result = {
        "className": "DelegatingController",
        "annotations": ["RestController"],
        "fields": [{"name": "orderService", "type": "OrderService"}],
        "methods": [
            {"name": "getOrder", "annotations": ["GetMapping"],
             "mappingPath": "/orders/{id}", "callsOnFields": ["orderService"]},
        ],
    }

    _apply_ast_result(comp, ast_result, "demo")
    assert comp.endpoints[0].calls == ["OrderService"]


# ── regression: this.field.foo() detection (P2a) ──────────────────────────────


def test_this_qualified_field_call_detected(tmp_path):
    """this.repo.findById(id) — receiver is this.repo, not plain repo. Must still detect."""
    src = tmp_path / "ThisQualifiedService.kt"
    src.write_text(
        "package demo\n"
        "import org.springframework.stereotype.Service\n"
        "@Service\n"
        "class ThisQualifiedService(private val repo: OrderRepository) {\n"
        "    fun find(id: String) = this.repo.findById(id)\n"
        "    fun save(o: Order) { this.repo.save(o) }\n"
        "}\n"
    )
    a = ast(src)
    assert a is not None
    methods = {m["name"]: m for m in a["methods"]}
    assert "repo" in methods["find"]["callsOnFields"]
    assert "repo" in methods["save"]["callsOnFields"]


# ── fieldCalls evidence packet tests ─────────────────────────────────────────


def test_field_calls_direct(tmp_path):
    """Direct call: fun create() = checkoutService.createSession()"""
    src = tmp_path / "DirectCall.kt"
    src.write_text(
        "package demo\nimport org.springframework.stereotype.Service\n"
        "@Service\nclass DirectCall(private val checkoutService: CheckoutService) {\n"
        "    fun create() = checkoutService.createSession()\n"
        "}\n"
    )
    a = ast(src)
    assert a is not None
    m = {m["name"]: m for m in a["methods"]}
    fc = m["create"]["fieldCalls"]
    assert len(fc) == 1
    assert fc[0] == {"field": "checkoutService", "type": "CheckoutService", "method": "createSession"}


def test_field_calls_multiple_methods_same_field(tmp_path):
    """Two calls on same field must both appear — dedup by (field, method), not field."""
    src = tmp_path / "MultiMethod.kt"
    src.write_text(
        "package demo\nimport org.springframework.stereotype.Service\n"
        "@Service\nclass MultiMethod(private val checkoutService: CheckoutService) {\n"
        "    fun handle() {\n"
        "        checkoutService.validate()\n"
        "        checkoutService.createSession()\n"
        "    }\n"
        "}\n"
    )
    a = ast(src)
    m = {m["name"]: m for m in a["methods"]}
    fc = m["handle"]["fieldCalls"]
    methods_called = [e["method"] for e in fc]
    assert "validate" in methods_called
    assert "createSession" in methods_called
    assert all(e["field"] == "checkoutService" for e in fc)


def test_field_calls_private_helper_transitive(tmp_path):
    """getOrder() → private buildResponse() → orderService.findById().
    getOrder.fieldCalls must include orderService.findById."""
    src = tmp_path / "TransitiveFC.kt"
    src.write_text(
        "package demo\nimport org.springframework.stereotype.Service\n"
        "@Service\nclass TransitiveFC(private val orderService: OrderService) {\n"
        "    fun getOrder(id: String) = buildResponse(id)\n"
        "    private fun buildResponse(id: String) = orderService.findById(id)\n"
        "}\n"
    )
    a = ast(src)
    m = {m["name"]: m for m in a["methods"]}
    fc_get = m["getOrder"]["fieldCalls"]
    assert any(e["field"] == "orderService" and e["method"] == "findById" for e in fc_get), \
        f"expected orderService.findById in fieldCalls, got: {fc_get}"
    # buildResponse directly calls it too
    fc_build = m["buildResponse"]["fieldCalls"]
    assert any(e["method"] == "findById" for e in fc_build)


def test_field_calls_this_receiver(tmp_path):
    """this.checkoutService.createSession() must produce the same fieldCall as direct call."""
    src = tmp_path / "ThisReceiver.kt"
    src.write_text(
        "package demo\nimport org.springframework.stereotype.Service\n"
        "@Service\nclass ThisReceiver(private val checkoutService: CheckoutService) {\n"
        "    fun create() = this.checkoutService.createSession()\n"
        "}\n"
    )
    a = ast(src)
    m = {m["name"]: m for m in a["methods"]}
    fc = m["create"]["fieldCalls"]
    assert len(fc) == 1
    assert fc[0]["field"] == "checkoutService"
    assert fc[0]["method"] == "createSession"


def test_field_calls_wrapper_false_positive(tmp_path):
    """wrapper.checkoutService.createSession() must NOT appear in fieldCalls."""
    src = tmp_path / "WrapperFC.kt"
    src.write_text(
        "package demo\nimport org.springframework.stereotype.Service\n"
        "@Service\nclass WrapperFC(private val checkoutService: CheckoutService) {\n"
        "    fun handle(wrapper: SomeWrapper) {\n"
        "        wrapper.checkoutService.createSession()  // NOT injected field\n"
        "    }\n"
        "    fun direct() = checkoutService.createSession()  // this IS\n"
        "}\n"
    )
    a = ast(src)
    m = {m["name"]: m for m in a["methods"]}
    assert m["handle"]["fieldCalls"] == [], \
        f"wrapper.field call should not appear, got: {m['handle']['fieldCalls']}"
    assert len(m["direct"]["fieldCalls"]) == 1


# ── regression: wrapper.field.foo() false positive (Codex P1) ────────────────


def test_wrapper_dot_field_not_counted_as_field_call(tmp_path):
    """
    wrapper.service.foo() must NOT be counted as a call on injected field 'service'.
    Only this.service.foo() and service.foo() should match.
    """
    src = tmp_path / "WrapperFalsePositive.kt"
    src.write_text(
        "package demo\n"
        "import org.springframework.stereotype.Service\n"
        "@Service\n"
        "class WrapperFalsePositive(private val service: OrderService) {\n"
        "    fun handle(wrapper: SomeWrapper) {\n"
        "        wrapper.service.doSomething()  // NOT a call on injected 'service'\n"
        "        service.realMethod()           // this IS a call on injected 'service'\n"
        "    }\n"
        "}\n"
    )
    a = ast(src)
    assert a is not None
    methods = {m["name"]: m for m in a["methods"]}
    calls = methods["handle"]["callsOnFields"]
    # service.realMethod() should be detected
    assert "service" in calls
    # wrapper.service.doSomething() should NOT add a second entry — but since
    # 'service' is already detected, check via the raw scanner that it appears once.
    # The main invariant: if the class had NO direct service.foo() calls,
    # wrapper.service.foo() alone must not produce a hit.


def test_wrapper_only_no_false_positive(tmp_path):
    """
    A method that ONLY calls wrapper.service.foo() with no direct service.foo()
    must NOT appear in callsOnFields for 'service'.
    """
    src = tmp_path / "WrapperOnly.kt"
    src.write_text(
        "package demo\n"
        "import org.springframework.stereotype.Service\n"
        "@Service\n"
        "class WrapperOnly(private val service: OrderService) {\n"
        "    fun handle(wrapper: SomeWrapper) {\n"
        "        wrapper.service.doSomething()  // only access via wrapper\n"
        "    }\n"
        "    fun direct() {\n"
        "        service.realMethod()  // direct call in a different method\n"
        "    }\n"
        "}\n"
    )
    a = ast(src)
    assert a is not None
    methods = {m["name"]: m for m in a["methods"]}
    assert "service" not in methods["handle"]["callsOnFields"]
    assert "service" in methods["direct"]["callsOnFields"]


# ── regression: nullable injected fields (Codex P1) ──────────────────────────


def test_nullable_constructor_param_extracted(tmp_path):
    """private val repo: Repo? — nullable type must still be extracted as 'Repo'."""
    src = tmp_path / "NullableService.kt"
    src.write_text(
        "package demo\n"
        "import org.springframework.stereotype.Service\n"
        "@Service\n"
        "class NullableService(\n"
        "    private val repo: OrderRepository?,\n"
        "    private val client: InventoryClient\n"
        ") {\n"
        "    fun find(id: String) = repo?.findById(id)\n"
        "    fun check() = client.ping()\n"
        "}\n"
    )
    a = ast(src)
    assert a is not None
    field_types = {f["type"] for f in a["fields"]}
    assert "OrderRepository" in field_types, "nullable Repo? must yield type 'OrderRepository'"
    assert "InventoryClient" in field_types
    methods = {m["name"]: m for m in a["methods"]}
    assert "repo" in methods["find"]["callsOnFields"]
    assert "client" in methods["check"]["callsOnFields"]
