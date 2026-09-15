import sys
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codemap.model import Component, Endpoint
from codemap.html import generate_html, _html_escape, _json_for_script, HTML_TEMPLATE


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


def _extract_js_function(name: str) -> str:
    start = HTML_TEMPLATE.index(f'function {name}')
    end = HTML_TEMPLATE.index("\ndocument.getElementById('mermaid-modal-close')", start)
    return HTML_TEMPLATE[start:end]


def _normalise_sequence_with_node(raw: str) -> str:
    if not shutil.which('node'):
        pytest.skip('node is required for frontend normalizer tests')
    js = (
        _extract_js_function('_normaliseSequenceDiagram')
        + '\nconst input = '
        + json.dumps(raw)
        + ';\nprocess.stdout.write(JSON.stringify(_normaliseSequenceDiagram(input)));'
    )
    result = subprocess.run(
        ['node', '-e', js],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_mermaid_normalizer_reuses_model_aliases():
    result = _normalise_sequence_with_node(
        '''
        sequenceDiagram
          participant U as User
          participant S as CustomerService
          U->>S: getCustomer
          S-->>U: customer
        '''
    )

    assert 'participant P1 as User' in result
    assert 'participant P2 as CustomerService' in result
    assert 'P1->>P2: getCustomer' in result
    assert 'P2-->>P1: customer' in result
    assert not re.search(r'participant P\d+ as U(?:\n|$)', result)


def test_mermaid_normalizer_drops_weak_abbreviation_participants():
    result = _normalise_sequence_with_node(
        '''
        sequenceDiagram
          participant CustomerManagementResource
          participant CustomerManagementService
          CustomerManagementResource->>CustomerManagementService: getCustomer
          CustomerManagementService-->>CS-: ignored response
          CS-->>CDAO-: ignored alias response
          CDAO-->>...: ignored ellipsis response
        '''
    )

    assert 'CustomerManagementResource' in result
    assert 'CustomerManagementService' in result
    assert 'CS-' not in result
    assert 'CDAO-' not in result
    assert '...' not in result


def test_mermaid_normalizer_caps_participant_count():
    result = _normalise_sequence_with_node(
        '''
        sequenceDiagram
          AService->>BService: call
          BService->>CService: call
          CService->>DService: call
          DService->>EService: call
          EService->>FService: call
          FService->>GService: call
          GService->>HService: call
        '''
    )

    assert result.count('participant ') == 6
    assert 'GService' not in result
    assert 'HService' not in result


def test_mermaid_normalizer_removes_visible_label_quotes():
    result = _normalise_sequence_with_node(
        '''
        sequenceDiagram
          participant C as "CustomerDao"
          participant S as “CustomerService”
          C->>S: findCustomer
        '''
    )

    assert 'CustomerDao' in result
    assert 'CustomerService' in result
    assert '"' not in result
