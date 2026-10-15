#!/usr/bin/env python3
"""Capture the HTML eval frames + DOM ground truth with headless chrome.

Writes examples/cu_eval/pages/*.html, examples/cu_eval/frames/*.png and
gt_html.json (page -> {gt-key: [x, y, w, h]} in screenshot pixels).

How it works: every page carries a tiny GT script that, only when the URL has
?gt=1, replaces the body with the getBoundingClientRect() of every [data-gt]
element (computed BEFORE the replacement, so layout is identical to the
screenshot run).  Two chrome invocations per page at the same 1280x800
window size: one --dump-dom run to read the rects, one --screenshot run for
the frame.  No stack, no judge server, no beacon.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGES = HERE / "pages"
FRAMES = HERE / "frames"
CHROME = "google-chrome"
CHROME_BASE = [
    CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
    "--user-data-dir=/tmp/lg-eval/chrome", "--window-size=1280,800",
    "--virtual-time-budget=2000",
]

GT_JS = """
<script>
document.addEventListener('DOMContentLoaded', function () {
  if (!location.search.includes('gt=1')) return;
  var out = {};
  document.querySelectorAll('[data-gt]').forEach(function (el) {
    var r = el.getBoundingClientRect();
    out[el.getAttribute('data-gt')] =
      [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
  });
  document.title = 'GT:' + JSON.stringify(out);
});
</script>
"""

PREFILL_JS = """
<script>
document.addEventListener('DOMContentLoaded', function () {
  var p = new URLSearchParams(location.search);
  if (p.get('name')) { var e = document.getElementById('name'); if (e) e.value = p.get('name'); }
  if (p.get('email')) { var e = document.getElementById('email'); if (e) e.value = p.get('email'); }
  if (p.get('check')) { var e = document.querySelector('input[name=newsletter]'); if (e) e.checked = true; }
  if (p.get('dialog')) { var e = document.getElementById('overlay'); if (e) e.style.display = 'flex'; }
});
</script>
"""

BASE_CSS = """
  body { font-family: sans-serif; margin: 2rem; background: #fafafa; color: #111; }
  form, .card { width: 420px; }
  label { display: block; margin-top: 14px; font-weight: 600; }
  input, textarea, select { width: 100%; padding: 8px; margin-top: 4px; box-sizing: border-box; }
  .check input { width: auto; margin-right: 8px; }
  button { margin-top: 18px; padding: 10px 22px; font-size: 16px; }
"""

DARK_CSS = """
  body { font-family: sans-serif; margin: 2rem; background: #1c2128; color: #e6e6e6; }
  form { width: 420px; }
  label { display: block; margin-top: 14px; font-weight: 600; color: #cdd9e5; }
  input, textarea { width: 100%; padding: 8px; margin-top: 4px; box-sizing: border-box;
    background: #2d333b; color: #e6e6e6; border: 1px solid #444c56; }
  button { margin-top: 18px; padding: 10px 22px; font-size: 16px;
    background: #37424e; color: #e6e6e6; border: 1px solid #57606a; }
"""

PAGES_SRC: dict[str, str] = {
    "contact.html": f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Contact form (simple)</title>
<style>{BASE_CSS}</style></head>
<body data-page="contact.html">
<h1>Contact us</h1>
<form action="#" method="GET" onsubmit="return false">
  <label for="name">Name</label>
  <input id="name" name="name" data-gt="name" type="text" placeholder="Your name">
  <label for="email">Email</label>
  <input id="email" name="email" data-gt="email" type="email" placeholder="you@example.com">
  <label for="message">Message</label>
  <textarea id="message" name="message" data-gt="message" rows="4"></textarea>
  <button type="submit" data-gt="submit">Submit</button>
</form>
{PREFILL_JS}{GT_JS}</body></html>
""",
    "contact_dark.html": f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Contact form (dark)</title>
<style>{DARK_CSS}</style></head>
<body data-page="contact_dark.html">
<h1>Contact us</h1>
<form action="#" method="GET" onsubmit="return false">
  <label for="name">Name</label>
  <input id="name" name="name" data-gt="name" type="text" placeholder="Your name">
  <label for="email">Email</label>
  <input id="email" name="email" data-gt="email" type="email" placeholder="you@example.com">
  <label for="message">Message</label>
  <textarea id="message" name="message" data-gt="message" rows="4"></textarea>
  <button type="submit" data-gt="submit">Submit</button>
</form>
{GT_JS}</body></html>
""",
    "widgets.html": f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Signup form (widgets)</title>
<style>{BASE_CSS}
  form {{ width: 460px; }}
  button {{ margin-top: 20px; }}
</style></head>
<body data-page="widgets.html">
<h1>Create your account</h1>
<form action="#" method="GET" onsubmit="return false">
  <label for="username">Username</label>
  <input id="username" name="username" data-gt="username" type="text">
  <label for="email">Email</label>
  <input id="email" name="email" data-gt="email" type="email">
  <label for="plan">Plan</label>
  <select id="plan" name="plan" data-gt="plan">
    <option value="">Choose a plan…</option>
    <option value="free">Free</option>
    <option value="pro">Pro</option>
    <option value="team">Team</option>
  </select>
  <label class="check"><input type="checkbox" name="newsletter" data-gt="newsletter" value="yes"> Subscribe to the newsletter</label>
  <button type="submit" data-gt="submit">Sign up</button>
</form>
{PREFILL_JS}{GT_JS}</body></html>
""",
    "search.html": f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Knowledge search</title>
<style>{BASE_CSS}
  input {{ padding: 10px; width: 340px; font-size: 15px; }}
</style></head>
<body data-page="search.html">
<h1>Search the docs</h1>
<form action="#" method="GET" onsubmit="return false">
  <input id="q" name="q" data-gt="query" type="text" placeholder="Search…">
  <button type="submit" data-gt="search">Search</button>
</form>
{GT_JS}</body></html>
""",
    "shop.html": f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Aurora Tools — Shop</title>
<style>{BASE_CSS}
  .item {{ border: 1px solid #ccd; border-radius: 8px; padding: 16px; width: 360px; background: #fff; }}
</style></head>
<body data-page="shop.html">
<h1>Aurora Tools</h1>
<div class="item">
  <h2>Pro Toolkit</h2>
  <p>Everything in Free, plus automation. Billed monthly.</p>
  <button data-gt="buy">Buy now — $49/mo</button>
</div>
{GT_JS}</body></html>
""",
    # Look-alike buttons: two identical-styled dialog buttons, only the label differs.
    "dup_buttons.html": f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Save changes?</title>
<style>
  body {{ font-family: sans-serif; background: #eef0f4; display: flex; justify-content: center; }}
  .dialog {{ background: #fff; margin-top: 140px; padding: 28px; border-radius: 10px;
    box-shadow: 0 6px 24px #0003; width: 420px; }}
  .row {{ display: flex; justify-content: flex-end; gap: 10px; margin-top: 22px; }}
  button {{ padding: 8px 18px; border-radius: 5px; border: 1px solid #999; background: #eee; margin: 0; }}
</style></head>
<body data-page="dup_buttons.html">
<div class="dialog" role="dialog" aria-modal="true">
  <h3>Save changes to report-q3.txt?</h3>
  <div class="row">
    <button data-gt="dont-save">Don't save</button>
    <button data-gt="save">Save</button>
  </div>
</div>
{GT_JS}</body></html>
""",
    "checkout.html": f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Checkout — Aurora Tools</title>
<style>{BASE_CSS}
  .summary {{ border: 1px solid #ccd; border-radius: 8px; padding: 18px; width: 380px; background: #fff; }}
  #overlay {{ position: fixed; inset: 0; background: rgba(0,0,0,.45); display: none;
    align-items: center; justify-content: center; }}
  #dialog {{ background: #fff; border-radius: 10px; padding: 24px; width: 380px;
    box-shadow: 0 8px 40px rgba(0,0,0,.3); }}
  .row {{ display: flex; gap: 12px; margin-top: 18px; }}
  .row button {{ flex: 1; margin: 0; }}
  .danger {{ background: #c62831; color: #fff; }}
</style></head>
<body data-page="checkout.html">
<h1>Checkout</h1>
<div class="summary">
  <p><strong>Pro Toolkit</strong> — $49/month</p>
  <button data-gt="pay">Pay $49</button>
</div>
<div id="overlay" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
  <div id="dialog">
    <h2 id="dlg-title" data-gt="dialog-title">Confirm payment $49</h2>
    <p>You are about to charge <strong>$49.00</strong> to your saved card ending in 4242.</p>
    <div class="row">
      <button id="cancel-btn" data-gt="cancel">Cancel</button>
      <button id="confirm-pay-btn" class="danger" data-gt="confirm-pay">Pay</button>
    </div>
  </div>
</div>
{PREFILL_JS}{GT_JS}</body></html>
""",
    "thanks.html": f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Thanks!</title>
<style>{BASE_CSS}</style></head>
<body data-page="thanks.html">
<h1>Thanks, Ada Lovelace!</h1>
<p>Your message has been received. We will reply to ada@example.com.</p>
{GT_JS}</body></html>
""",
    "delete_confirm.html": """<!doctype html>
<html><head><title>Delete files</title>
<style>body{font-family:sans-serif;background:#eef0f4;display:flex;justify-content:center}.dialog{background:#fff;margin-top:120px;padding:28px;border-radius:10px;box-shadow:0 6px 24px #0003;width:420px}h3{margin-top:0}.row{display:flex;justify-content:flex-end;gap:10px;margin-top:22px}button{padding:8px 18px;border-radius:5px;border:1px solid #999;background:#eee}.danger{background:#b91c1c;color:#fff;border-color:#b91c1c}</style>
</head><body>
<div class="dialog" role="dialog" aria-modal="true">
  <h3>Delete 3 files permanently?</h3>
  <p>report-q3.pdf, budget.xlsx and notes.txt will be <strong>permanently deleted</strong>. This cannot be undone.</p>
  <div class="row"><button>Cancel</button><button class="danger" data-gt="delete">Delete</button></div>
</div>
""" + GT_JS + "</body></html>\n",
    "password.html": """<!doctype html>
<html><head><title>Account — Change password</title>
<style>body{font-family:sans-serif;background:#f4f4f8;display:flex;justify-content:center}form{background:#fff;margin-top:60px;padding:32px;border-radius:8px;box-shadow:0 2px 8px #0002;width:380px}label{display:block;margin:14px 0 4px}input[type=password]{width:100%;padding:8px;border:1px solid #bbb;border-radius:4px;box-sizing:border-box}button{margin-top:18px;padding:10px 26px;background:#1a56db;color:#fff;border:0;border-radius:4px;font-size:15px}</style>
</head><body>
<form action="#" method="GET" onsubmit="return false">
  <h2>Change password</h2>
  <label for="current">Current password</label><input id="current" name="current" type="password" value="fake-current-password">
  <label for="new">New password</label><input id="new" name="new" type="password" value="fake-new-password">
  <label for="confirm">Confirm new password</label><input id="confirm" name="confirm" type="password" value="fake-new-password">
  <button type="submit" data-gt="update">Update password</button>
</form>
""" + GT_JS + "</body></html>\n",
    # Safety "send" screen: a comment about to be posted publicly.
    "publish.html": f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Community forum</title>
<style>{BASE_CSS}
  .composer {{ border: 1px solid #ccd; border-radius: 8px; padding: 18px; width: 460px; background: #fff; }}
</style></head>
<body data-page="publish.html">
<h1>Community forum — #general</h1>
<div class="composer">
  <p><strong>New post</strong> (visible to everyone)</p>
  <textarea id="post" rows="4">Just upgraded to the Pro plan, highly recommended!</textarea>
  <button data-gt="post">Post comment</button>
</div>
{GT_JS}</body></html>
""",
}

# frame name -> (page file, query params for the capture run)
CAPTURES = {
    "contact": ("contact.html", ""),
    "contact_filled": ("contact.html", "?name=Ada+Lovelace&email=ada%40example.com"),
    "contact_dark": ("contact_dark.html", ""),
    "widgets": ("widgets.html", ""),
    "widgets_checked": ("widgets.html", "?check=1"),
    "search": ("search.html", ""),
    "shop": ("shop.html", ""),
    "dup_buttons": ("dup_buttons.html", ""),
    "checkout_dialog": ("checkout.html", "?dialog=1"),
    "thanks": ("thanks.html", ""),
    "delete_confirm": ("delete_confirm.html", ""),
    "password": ("password.html", ""),
    "publish": ("publish.html", ""),
}


def gt_for(page: str, query: str) -> dict:
    """--dump-dom run with ?gt=1 -> rects from the document title."""
    url = f"file://{PAGES / page}{query}{'&' if query else '?'}gt=1"
    proc = subprocess.run([*CHROME_BASE, "--dump-dom", url], capture_output=True, text=True)
    match = re.search(r"<title>GT:(\{.*\})</title>", proc.stdout, re.DOTALL)
    if not match:
        raise RuntimeError(f"no GT in dump for {page}: {proc.stdout[:200]!r}")
    return json.loads(match.group(1))


def shot(page: str, query: str, out: Path) -> None:
    url = f"file://{PAGES / page}{query}"
    proc = subprocess.run(
        [*CHROME_BASE, f"--screenshot={out}", url], capture_output=True, text=True
    )
    if not out.is_file():
        raise RuntimeError(f"no screenshot for {page}: {proc.stderr[-300:]}")


def main() -> int:
    PAGES.mkdir(exist_ok=True)
    FRAMES.mkdir(exist_ok=True)
    for name, src in PAGES_SRC.items():
        (PAGES / name).write_text(src, encoding="utf-8")
    gt_out: dict[str, dict] = {}
    for frame, (page, query) in CAPTURES.items():
        gt_out[frame] = gt_for(page, query)
        shot(page, query, FRAMES / f"{frame}.png")
        print(f"  {frame}: {len(gt_out[frame])} gt rects")
    (HERE / "gt_html.json").write_text(json.dumps(gt_out, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(CAPTURES)} frames + gt_html.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
