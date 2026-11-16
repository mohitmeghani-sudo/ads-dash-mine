"""Build step for Meta Ads clients (clients.json: "platform": "meta"). Called from build.py.

Pulls via meta_api, saves each ad's thumbnail next to the page (Meta's CDN links expire, and the published page
can't hotlink them), and writes dist/<client>/index.html + data.json from dashboard/meta_template.html.
"""
import hashlib
import io
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from PIL import Image

import meta_api

ROOT = Path(__file__).parent
THUMB_PX = 720


def _download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def save_thumb(url, folder):
    """-> 'assets/<hash>.webp' or '' when the image can't be fetched (the card then shows a placeholder)."""
    if not url:
        return ""
    key = hashlib.sha1(url.split("?")[0].encode()).hexdigest()[:16]
    target = folder / f"{key}.webp"
    if target.exists():
        return f"assets/{target.name}"
    for _ in range(3):
        try:
            im = Image.open(io.BytesIO(_download(url)))
            im.thumbnail((THUMB_PX, THUMB_PX))
            im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=80, method=4)
            target.write_bytes(buf.getvalue())
            return f"assets/{target.name}"
        except Exception:
            continue
    return ""


def main(key, client, dist):
    data = meta_api.load_or_pull(key, client)
    dist = Path(dist)
    assets = dist / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(6) as ex:
        thumbs = list(ex.map(lambda a: save_thumb(a.get("thumbUrl"), assets), data["ads"]))
    ads = []
    for a, t in zip(data["ads"], thumbs):
        a = {k: v for k, v in a.items() if k != "thumbUrl"}
        a["thumb"] = t
        ads.append(a)
    missing = sum(1 for a in ads if not a["thumb"])
    payload = {
        "client": client["name"], "platform": "meta", "domain": client.get("domain", ""),
        "liveUrl": client.get("live_url", ""), "sample": bool(data.get("sample")),
        "account": data["account"], "dateRange": data.get("dateRange", ""),
        "pulledAt": data["pulledAt"], "builtAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "targets": client.get("meta_targets", {}),  # optional: {"cpa": 40, "roas": 2.5, "hookRate": 0.25}
        # naming rules the page uses to group ads into creatives / creators (see clients.json)
        "config": {"creators": client.get("meta_creators", []), "skus": client.get("meta_skus", []),
                   "overrides": client.get("meta_overrides", {})},
        "ads": ads, "daily": data.get("daily", []),
    }
    template = (ROOT / "dashboard" / "meta_template.html").read_text(encoding="utf-8").replace("__CLIENT__", client["name"])
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = template.replace("/*__DATA__*/null", blob)
    dist.mkdir(parents=True, exist_ok=True)
    tmp = dist / "index.html.tmp"
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(dist / "index.html")  # atomic swap: the server never serves a half-written page
    (dist / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    video = sum(1 for a in ads if a["format"] == "Video")
    print(f"Meta ads: {len(ads)} ({video} video), thumbnails missing: {missing}, html KB: {len(html.encode()) // 1024}")
