import sys
import argparse
import subprocess
import time
import threading
import os
import signal
import http.server
import functools
import webbrowser
from pathlib import Path

from .scan import scan
from .resolve import resolve
from .serve import run_once

_SERVE_DIR = Path.home() / '.codemap' / 'serve'


def _resolve_source_root(given: Path) -> Path:
    """Walk down to the Spring source root if the user passes a repo/module root."""
    if not given.exists():
        print(f'Error: {given} does not exist', file=sys.stderr)
        sys.exit(1)
    # Try standard Spring Boot source layouts in preference order
    for candidate in [
        given / 'src' / 'main',
        given / 'src' / 'main' / 'kotlin',
        given / 'src' / 'main' / 'java',
    ]:
        if candidate.exists():
            print(f'Auto-detected source root: {candidate}', file=sys.stderr)
            return candidate
    # Passed path is already the source root (or an explicit non-standard layout)
    return given


def main() -> None:
    p = argparse.ArgumentParser(
        description='Generate an interactive Spring Boot architecture map.'
    )
    p.add_argument('root', nargs='?', default='.', help='Source root (default: .)')
    p.add_argument('--html', default='appmap.html', help='HTML output (default: appmap.html)')
    p.add_argument('--md', default='architecture.md', help='Markdown output (default: architecture.md)')
    p.add_argument('--title', default='', help='Map title (default: project directory name)')
    p.add_argument('--name', default='', metavar='SLUG', help='Project slug for multi-project server (e.g. shippingguide)')
    p.add_argument('--docs', default='', metavar='DIR', help='Write one markdown file per endpoint into DIR')
    p.add_argument('--no-html', action='store_true', help='Skip HTML generation')
    p.add_argument('--no-md',   action='store_true', help='Skip Markdown generation')
    p.add_argument('--list', action='store_true', help='Print component table to stdout')
    p.add_argument('--serve', action='store_true', help='Serve HTML via localhost and open in browser')
    p.add_argument('--port', type=int, default=8742, help='Port for --serve (default: 8742)')
    p.add_argument('--watch', action='store_true', help='Re-scan every --interval seconds')
    p.add_argument('--interval', type=int, default=120, help='Watch interval in seconds (default: 120)')
    args = p.parse_args()

    root = _resolve_source_root(Path(args.root))

    # Auto-fill title from directory name if not provided
    if not args.title:
        args.title = Path(args.root).resolve().name.replace('-', ' ').replace('_', ' ').title()

    if args.list:
        components, _, _ = scan(root)
        resolve(components)
        visible = [c for c in components if c.kind not in ('CONFIG',)]
        print(f'{"Component":<42} {"Kind":<12} {"Domain":<18} {"External"}')
        print('─' * 90)
        for c in sorted(visible, key=lambda x: (x.domain, x.kind, x.name)):
            ext = ', '.join(c.external_systems) or '—'
            print(f'{c.name:<42} {c.kind:<12} {c.domain or "—":<18} {ext}')
        return

    if args.html != 'appmap.html':
        html_path = Path(args.html)
    elif args.name:
        html_path = _SERVE_DIR / args.name / 'index.html'
    else:
        html_path = _SERVE_DIR / 'index.html'

    if args.serve and not args.no_html:
        # Always serve from the _SERVE_DIR root so all projects + landing page are reachable
        serve_dir = str(_SERVE_DIR.resolve())
        _SERVE_DIR.mkdir(parents=True, exist_ok=True)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=serve_dir)
        handler.log_message = lambda *a: None  # type: ignore
        port = args.port
        try:
            result = subprocess.run(['lsof', '-ti', f'tcp:{port}'], capture_output=True, text=True)
            for pid in result.stdout.strip().split():
                try: os.kill(int(pid), signal.SIGTERM)
                except Exception: pass
            time.sleep(0.3)
        except Exception:
            pass
        try:
            server = http.server.HTTPServer(('localhost', port), handler)
        except OSError:
            print(f'Could not bind to port {port}', file=sys.stderr)
            sys.exit(1)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base_url = f'http://localhost:{port}'
        open_url = f'{base_url}/{args.name}/' if args.name else f'{base_url}/'
        print(f'Serving at {base_url}/', file=sys.stderr)
        webbrowser.open(open_url)

        run_once(args, root, html_path)
        if args.watch:
            print(f'Watching — re-scanning every {args.interval}s. Ctrl+C to stop.', file=sys.stderr)
            try:
                while True:
                    time.sleep(args.interval)
                    run_once(args, root, html_path)
            except KeyboardInterrupt:
                pass
        else:
            try:
                while True: time.sleep(1)
            except KeyboardInterrupt:
                pass
        server.shutdown()
    else:
        run_once(args, root, html_path)
        if args.watch:
            print(f'Watching — re-scanning every {args.interval}s. Ctrl+C to stop.', file=sys.stderr)
            try:
                while True:
                    time.sleep(args.interval)
                    run_once(args, root, html_path)
            except KeyboardInterrupt:
                pass
