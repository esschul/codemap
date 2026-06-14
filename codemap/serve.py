import json
import datetime
import time as _time
from pathlib import Path

from .model import Component
from .scan import scan
from .resolve import resolve, ResolveDiagnostics, format_diagnostics
from .html import generate_html
from .markdown import generate_markdown, generate_endpoint_docs, _generate_docs_index


def _generate_landing_page(serve_dir: Path) -> None:
    """Scan serve_dir for project subdirs and write a landing page index.html."""
    projects = []
    for sub in sorted(serve_dir.iterdir()):
        v = sub / 'version.json'
        if sub.is_dir() and (sub / 'index.html').exists() and v.exists():
            try:
                meta = json.loads(v.read_text())
                meta['slug'] = sub.name
                projects.append(meta)
            except Exception:
                pass
    projects.sort(key=lambda p: p.get('ts', 0), reverse=True)

    def _fmt_ts(ts: int) -> str:
        return datetime.datetime.fromtimestamp(ts).strftime('%d %b %Y %H:%M') if ts else '—'

    cards_html = ''
    for p in projects:
        domains = ' '.join(f'<span class="lp-chip">{d}</span>' for d in (p.get('domains') or [])[:8])
        ext_count = len(p.get('externals') or [])
        cards_html += f'''
        <a class="lp-card" href="/{p['slug']}/">
          <div class="lp-card-title">{p.get('title', p['slug'])}</div>
          <div class="lp-card-stats">
            <span>{p.get('controllers', 0)} controllers</span>
            <span>{p.get('services', 0)} services</span>
            <span>{p.get('endpoints', 0)} endpoints</span>
            <span>{ext_count} external</span>
          </div>
          <div class="lp-card-domains">{domains}</div>
          <div class="lp-card-ts">Scanned {_fmt_ts(p.get('ts', 0))}</div>
        </a>'''

    empty = '<div class="lp-empty">No projects scanned yet.<br>Run springmap with <code>--name &lt;slug&gt;</code> to add one.</div>' if not projects else ''

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>codemap</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  background:#0b1120;color:#e2e8f0;min-height:100vh}}
header{{background:#111827;border-bottom:1px solid #1f2937;padding:16px 24px;
  display:flex;align-items:center;gap:12px}}
header h1{{font-size:18px;font-weight:700;color:#e2e8f0}}
header p{{font-size:12px;color:#6b7280;margin-left:auto}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));
  gap:16px;padding:24px;max-width:1200px;margin:0 auto}}
.lp-card{{background:#111827;border:1px solid #1f2937;border-radius:10px;
  padding:18px;text-decoration:none;color:inherit;display:block;
  transition:border-color .15s,transform .15s}}
.lp-card:hover{{border-color:#3b82f6;transform:translateY(-1px)}}
.lp-card-title{{font-size:15px;font-weight:700;color:#e2e8f0;margin-bottom:8px}}
.lp-card-stats{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px}}
.lp-card-stats span{{font-size:11px;color:#6b7280;background:#1f2937;
  padding:2px 8px;border-radius:4px}}
.lp-card-domains{{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px}}
.lp-chip{{font-size:10px;background:#172554;color:#60a5fa;border:1px solid #1e3a8a;
  padding:1px 6px;border-radius:4px}}
.lp-card-ts{{font-size:10px;color:#4b5563}}
.lp-empty{{text-align:center;padding:80px 24px;color:#4b5563;font-size:14px;line-height:2}}
.lp-empty code{{background:#1f2937;padding:2px 6px;border-radius:4px;color:#94a3b8}}
</style>
</head>
<body>
<header>
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2">
    <circle cx="12" cy="12" r="3"/><line x1="12" y1="2" x2="12" y2="6"/>
    <line x1="12" y1="18" x2="12" y2="22"/><line x1="2" y1="12" x2="6" y2="12"/>
    <line x1="18" y1="12" x2="22" y2="12"/>
  </svg>
  <h1>codemap</h1>
  <p>{len(projects)} project{'s' if len(projects) != 1 else ''}</p>
</header>
<div class="grid">
  {cards_html}
  {empty}
</div>
</body>
</html>'''

    (serve_dir / 'index.html').write_text(html, encoding='utf-8')


def run_once(args, root: Path, html_path: Path) -> None:
    """Scan and write all requested outputs. Called on every watch iteration."""
    import sys
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] Scanning {root}…', file=sys.stderr)
    components, scan_warnings, ast_enriched = scan(root)
    diag = ResolveDiagnostics() if getattr(args, 'debug_resolve', False) else None
    iface_map = resolve(components, diagnostics=diag)

    visible = [c for c in components if c.kind not in ('CONFIG',)]
    print(f'Found {len(visible)} components.', file=sys.stderr)

    if diag is not None:
        print(format_diagnostics(diag), file=sys.stderr)

    if not visible:
        print('No Spring components detected. Is this a Spring Boot project?', file=sys.stderr)
        return

    if not args.no_html:
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html = generate_html(visible, args.title, scan_root=root,
                             warnings=scan_warnings, ast_enriched=ast_enriched,
                             iface_map=iface_map)
        html_path.write_text(html, encoding='utf-8')
        # Write rich version.json for polling + landing page metadata
        controllers = [c for c in visible if c.endpoints]
        version_meta = {
            'ts': int(_time.time()),
            'components': len(visible),
            'title': args.title,
            'controllers': len(controllers),
            'services': sum(1 for c in visible if c.kind == 'SERVICE'),
            'endpoints': sum(len(c.endpoints) for c in controllers),
            'domains': sorted({c.domain for c in visible if c.domain}),
            'externals': sorted({e for c in visible for e in c.external_systems}),
        }
        (html_path.parent / 'version.json').write_text(json.dumps(version_meta), encoding='utf-8')
        print(f'Written → {html_path}', file=sys.stderr)
        # Regenerate landing page if this is a named project
        if getattr(args, 'name', ''):
            _generate_landing_page(html_path.parent.parent)

    if not args.no_md:
        md = generate_markdown(visible)
        Path(args.md).write_text(md, encoding='utf-8')
        print(f'Written → {args.md}', file=sys.stderr)

    if args.docs:
        docs_dir = Path(args.docs)
        n = generate_endpoint_docs(visible, docs_dir, title=args.title)
        _generate_docs_index(docs_dir, args.title, visible)
        print(f'Written → {docs_dir}/ ({n} endpoint files + index.md)', file=sys.stderr)
