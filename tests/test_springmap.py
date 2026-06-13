import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codemap.scan import parse_file
from codemap.html import generate_html
from codemap.model import Component, Endpoint


class _SpringmapShim:
    """Namespace shim so existing tests using springmap.X still work."""
    parse_file = staticmethod(parse_file)
    generate_html = staticmethod(generate_html)
    Component = Component
    Endpoint = Endpoint

springmap = _SpringmapShim()


def parse_kotlin(source: str):
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "DemoController.kt"
    path.write_text(source, encoding="utf-8")
    return springmap.parse_file(path)


def test_mapping_annotation_is_not_reused_for_following_helper_method():
    comp = parse_kotlin(
        """
        package com.example.demo

        import org.springframework.web.bind.annotation.GetMapping
        import org.springframework.web.bind.annotation.RequestMapping
        import org.springframework.web.bind.annotation.RestController

        @RestController
        @RequestMapping("/api")
        class DemoController(private val service: DemoService) {
            @GetMapping("/real")
            fun real() = service.call()

            fun helper() = "not an endpoint"
        }

        class DemoService {
            fun call() = "ok"
        }
        """
    )

    assert [(e.http_method, e.path, e.handler) for e in comp.endpoints] == [
        ("GET", "/api/real", "real")
    ]


def test_mapping_survives_intermediate_swagger_annotation():
    comp = parse_kotlin(
        """
        package com.example.demo

        import io.swagger.v3.oas.annotations.Operation
        import org.springframework.web.bind.annotation.GetMapping
        import org.springframework.web.bind.annotation.RestController

        @RestController
        class DemoController {
            @GetMapping("/real")
            @Operation(summary = "Real endpoint")
            fun real() = "ok"
        }
        """
    )

    assert [(e.http_method, e.path, e.handler) for e in comp.endpoints] == [
        ("GET", "/real", "real")
    ]


def test_html_escapes_title_and_repo_derived_values():
    comp = springmap.Component(
        name="DemoController",
        kind="CONTROLLER",
        package="com.example.demo",
        file="DemoController.kt",
        domain="<domain>",
        endpoints=[
            springmap.Endpoint(
                http_method="GET",
                path='/<script>alert("x")</script>',
                handler="handle",
            )
        ],
    )

    html = springmap.generate_html([comp], title="<App>")

    assert "<title>&lt;App&gt;</title>" in html
    assert "${escHtml(ep.path)}" in html
    assert "${escHtml(ctrl.domain)}" in html
    assert "/<script>alert" not in html


def test_java_ast_scanner_ignores_non_injected_fields():
    if not shutil.which("java"):
        return

    jar = ROOT / "ast_scanner" / "target" / "ast-scanner.jar"
    tmp = Path(tempfile.mkdtemp())
    source = tmp / "DemoController.java"
    source.write_text(
        """
        package com.example.demo;

        public class DemoController {
            private final DemoService service;
            private Helper helper = new Helper();

            public DemoController(DemoService service) {
                this.service = service;
            }

            public String handle() {
                helper.format();
                return service.call();
            }
        }
        """,
        encoding="utf-8",
    )

    proc = subprocess.run(
        ["java", "-jar", str(jar)],
        input=str(source),
        capture_output=True,
        text=True,
        check=True,
    )

    data = json.loads(proc.stdout)
    assert data[0]["fields"] == [{"name": "service", "type": "DemoService"}]
    assert data[0]["methods"][0]["callsOnFields"] == ["service"]
