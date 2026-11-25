"""Regenerate the README screenshots in docs/images/ from the demo app.

The demo app is tests/fixture-spring. It is copied outside the git checkout
first so the map footer does not show a local branch name.

Needs Playwright with Chromium (dev-only, not a codemap dependency):

    pip install playwright && playwright install chromium
    python scripts/readme_screenshots.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs' / 'images'


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    demo = tmp / 'shop-demo'
    shutil.copytree(ROOT / 'tests' / 'fixture-spring', demo)
    html = tmp / 'index.html'
    subprocess.run([sys.executable, '-c', 'from codemap.cli import main; main()',
                    str(demo), '--title', 'Shop Demo', '--no-md', '--html', str(html)],
                   check=True, cwd=ROOT)

    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1600, 'height': 640}, device_scale_factor=2)
        page.goto(html.as_uri())
        page.wait_for_timeout(800)

        # Chain view for POST /api/v1/orders, inspector collapsed for full width
        page.get_by_text('placeOrder()').first.click()
        page.locator('.inspector-toggle').first.click()
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / 'chain.png'))

        # Full component graph
        page.set_viewport_size({'width': 1760, 'height': 560})
        page.reload()
        page.wait_for_timeout(800)
        page.get_by_role('button', name='Graph').first.click()
        page.wait_for_timeout(1200)
        page.screenshot(path=str(OUT / 'graph.png'))

        # External systems overlay
        page.reload()
        page.wait_for_timeout(800)
        page.set_viewport_size({'width': 1600, 'height': 420})
        page.get_by_role('button', name='Externals').first.click()
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT / 'externals.png'))
        browser.close()
    print(f'Wrote {", ".join(p.name for p in sorted(OUT.glob("*.png")))} to {OUT}')


if __name__ == '__main__':
    main()
