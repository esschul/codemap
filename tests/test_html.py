import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codemap.model import Component, Endpoint
from codemap.html import generate_html, _html_escape, _json_for_script


def _ctrl(name='Ctrl', endpoints=None, domain=''):
    c = Component(name=name, kind='CONTROLLER', package='com.example', file=f'{name}.kt', domain=domain)
    c.endpoints = endpoints or []
    return c


def test_html_escape_ampersand():
    assert _html_escape('a & b') == 'a &amp; b'


def test_html_escape_tags():
    assert _html_escape('<script>') == '&lt;script&gt;'


def test_html_escape_quotes():
    assert _html_escape('"hello"') == '&quot;hello&quot;'
    assert _html_escape("it's") == 'it&#39;s'


def test_json_for_script_escapes_lt_gt():
    result = _json_for_script('<script>')
    assert '<' not in result
    assert '>' not in result
    assert '\\u003c' in result
    assert '\\u003e' in result


def test_json_for_script_escapes_ampersand():
    result = _json_for_script('a & b')
    assert '&' not in result
    assert '\\u0026' in result


def test_generate_html_title_escaped():
    html = generate_html([_ctrl()], title='<App & Co>')
    assert '<title>&lt;App &amp; Co&gt;</title>' in html


def test_generate_html_stats_line_counts():
    ctrl = _ctrl(endpoints=[
        Endpoint(http_method='GET', path='/a', handler='a'),
        Endpoint(http_method='POST', path='/b', handler='b'),
    ])
    svc = Component(name='Svc', kind='SERVICE', package='com.example', file='Svc.kt')
    html = generate_html([ctrl, svc], title='Test')
    # stats string embedded in the page
    assert '1 controllers' in html
    assert '1 services' in html
    assert '2 endpoints' in html


def test_generate_html_contains_sidebar_data():
    ctrl = _ctrl(endpoints=[Endpoint(http_method='GET', path='/ping', handler='ping')])
    html = generate_html([ctrl], title='Test')
    assert 'SIDEBAR_DATA' not in html   # placeholder replaced
    assert '/ping' in html


def test_generate_html_xss_path_not_raw_in_html():
    xss = '/<script>alert(1)</script>'
    ctrl = _ctrl(endpoints=[Endpoint(http_method='GET', path=xss, handler='h')])
    html = generate_html([ctrl], title='Test')
    assert xss not in html


def test_generate_html_warnings_embedded():
    html = generate_html([_ctrl()], title='T', warnings=['something went wrong'])
    assert 'something went wrong' in html


def test_generate_html_scan_meta_version():
    from codemap.html import _SCANNER_VERSION
    html = generate_html([_ctrl()], title='T')
    assert _SCANNER_VERSION in html
