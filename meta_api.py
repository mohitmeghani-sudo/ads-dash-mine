"""Meta (Facebook / Instagram) Ads source for the dashboard, via the Marketing API.

`load_or_pull(key, client)` returns one dict:
    {"account": {...}, "ads": [...], "pulledAt": iso}
Each ad carries its creative (copy, format, thumbnail, preview link) and 30-day insights, including the video
retention counts (3-second plays, ThruPlay, 25/50/75/95/100 % watched) the creative dashboard is built on.

Credentials come from env vars (Railway) or ~/.meta_creds.json (local); never commit them:
    META_ACCESS_TOKEN   long-lived user token or a system-user token with ads_read (and read_insights)
    META_AD_ACCOUNT_ID  optional default, e.g. act_1234567890 (clients.json "meta_ad_account_id" wins)
    META_API_VERSION    optional, default v23.0

Set META_SAMPLE=1 to build from made-up data (local layout testing only; the page is labelled SAMPLE DATA).
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
VERSION = os.environ.get("META_API_VERSION", "v23.0")
GRAPH = f"https://graph.facebook.com/{VERSION}"
DATE_PRESET = "last_30d"
CACHE_VERSION = 1
CACHE_HOURS = float(os.environ.get("META_CACHE_HOURS", "3"))
_VOLUME = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
CACHE_DIR = Path(os.environ.get("META_CACHE_DIR") or (Path(_VOLUME) / "meta_cache" if _VOLUME else ROOT / "cache"))

AD_FIELDS = ("id,name,status,effective_status,created_time,campaign{id,name,objective},adset{id,name},"
             "creative{id,name,title,body,call_to_action_type,object_type,thumbnail_url,image_url,video_id,"
             "object_story_spec,asset_feed_spec,instagram_permalink_url,effective_object_story_id},"
             "preview_shareable_link")
VIDEO_ACTIONS = {  # insights field -> our key
    "video_p25_watched_actions": "p25", "video_p50_watched_actions": "p50", "video_p75_watched_actions": "p75",
    "video_p95_watched_actions": "p95", "video_p100_watched_actions": "p100",
    "video_thruplay_watched_actions": "thruplay", "video_30_sec_watched_actions": "s30",
}
INSIGHT_FIELDS = ",".join(["ad_id", "spend", "impressions", "reach", "frequency", "clicks", "inline_link_clicks",
                           "ctr", "cpc", "cpm", "actions", "action_values", "purchase_roas",
                           "video_avg_time_watched_actions"] + list(VIDEO_ACTIONS))


# ----------------------------------------------------------------- credentials
def _creds():
    clean = lambda v: (v or "").strip().strip("\"'").strip()
    token = clean(os.environ.get("META_ACCESS_TOKEN"))
    account = clean(os.environ.get("META_AD_ACCOUNT_ID"))
    local = Path.home() / ".meta_creds.json"
    if not token and local.exists():
        try:
            d = json.loads(local.read_text(encoding="utf-8"))
            token, account = clean(d.get("access_token")), account or clean(d.get("ad_account_id"))
        except (OSError, ValueError):
            pass
    return {"token": token, "account": account} if token else None


def sample_mode():
    return os.environ.get("META_SAMPLE", "").strip().lower() in ("1", "on", "true", "yes")


def available():
    return sample_mode() or _creds() is not None


def act_id(client, creds=None):
    raw = client.get("meta_ad_account_id") or (creds or _creds() or {}).get("account") or ""
    digits = re.sub(r"\D", "", raw)
    if not digits:
        raise RuntimeError("No Meta ad account: set meta_ad_account_id in clients.json or META_AD_ACCOUNT_ID")
    return "act_" + digits


# -------------------------------------------------------------------- client
class Api:
    def __init__(self, token):
        self.token, self.requests = token, 0

    def get(self, path, params=None, url=None):
        """One Graph call. Retries rate limits and transient 5xx; any other error is raised with Meta's message."""
        if url is None:
            q = {**(params or {}), "access_token": self.token}
            url = f"{GRAPH}/{path.lstrip('/')}?{urllib.parse.urlencode(q)}"
        for attempt in range(5):
            self.requests += 1
            try:
                with urllib.request.urlopen(urllib.request.Request(url), timeout=90) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                try:
                    err = json.loads(body).get("error", {})
                except ValueError:
                    err = {}
                code = err.get("code")
                if (e.code >= 500 or code in (1, 2, 4, 17, 32, 613, 80000, 80004)) and attempt < 4:
                    time.sleep(min(60, 5 * 2 ** attempt))  # rate limit / hiccup: back off, then retry
                    continue
                msg = err.get("message") or body[:300]
                raise RuntimeError(f"Meta API {e.code}{f' (code {code})' if code else ''}: {msg}") from None
            except urllib.error.URLError:
                if attempt == 4:
                    raise
                time.sleep(3 * (attempt + 1))

    def pages(self, path, params):
        """Follow paging.next until exhausted."""
        out, res = [], self.get(path, {**params, "limit": params.get("limit", 100)})
        while True:
            out += res.get("data", [])
            nxt = (res.get("paging") or {}).get("next")
            if not nxt:
                return out
            res = self.get(None, url=nxt)


def _n(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _action(rows, *types):
    """First matching action_type's value from an insights actions-style list."""
    by = {r.get("action_type"): _n(r.get("value")) for r in (rows or [])}
    for t in types:
        if t in by:
            return by[t]
    return 0.0


PURCHASE = ("omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase")
LEAD = ("lead", "onsite_conversion.lead_grouped", "offsite_conversion.fb_pixel_lead", "omni_lead")


def result_types(kind):
    return {"purchase": PURCHASE, "lead": LEAD}.get(kind) or (kind,)


def parse_insights(row, result_kind):
    """One insights row -> the flat numbers the dashboard uses (counts, not rates; rates are derived in the page)."""
    spend, impr = _n(row.get("spend")), _n(row.get("impressions"))
    actions = row.get("actions")
    rtypes = result_types(result_kind)
    results = _action(actions, *rtypes)
    value = _action(row.get("action_values"), *PURCHASE)
    video = {k: _action(row.get(f), "video_view") for f, k in VIDEO_ACTIONS.items()}
    return {
        "spend": spend, "impr": impr, "reach": _n(row.get("reach")), "clicks": _n(row.get("clicks")),
        "linkClicks": _n(row.get("inline_link_clicks")), "results": results, "value": value,
        "plays3s": _action(actions, "video_view"),  # Meta's "3-second video plays"
        "avgWatch": _action(row.get("video_avg_time_watched_actions"), "video_view"),  # seconds
        **video,
        "addToCart": _action(actions, "omni_add_to_cart", "add_to_cart"),
    }


# ----------------------------------------------------------------- creatives
def first_line(text, limit=140):
    t = re.sub(r"\s+", " ", (text or "").strip())
    m = re.split(r"(?<=[.!?])\s|\n", (text or "").strip(), maxsplit=1)
    line = re.sub(r"\s+", " ", m[0]).strip() if m else t
    return (line[:limit - 1] + "…") if len(line) > limit else line


def creative_fields(cr):
    """Primary text / headline / CTA / format from the many places Meta stores them."""
    cr = cr or {}
    spec = cr.get("object_story_spec") or {}
    feed = cr.get("asset_feed_spec") or {}
    link = spec.get("link_data") or {}
    vid = spec.get("video_data") or {}
    photo = spec.get("photo_data") or {}
    body = cr.get("body") or link.get("message") or vid.get("message") or photo.get("caption") or ""
    title = cr.get("title") or link.get("name") or vid.get("title") or ""
    desc = link.get("description") or vid.get("link_description") or ""
    cta = cr.get("call_to_action_type") or (link.get("call_to_action") or {}).get("type") \
        or (vid.get("call_to_action") or {}).get("type") or ""
    url = (link.get("link") or ((vid.get("call_to_action") or {}).get("value") or {}).get("link")
           or ((link.get("call_to_action") or {}).get("value") or {}).get("link") or "")
    bodies = [b.get("text") for b in feed.get("bodies", []) if b.get("text")]
    titles = [t.get("text") for t in feed.get("titles", []) if t.get("text")]
    body = body or (bodies[0] if bodies else "")
    title = title or (titles[0] if titles else "")
    video_id = cr.get("video_id") or vid.get("video_id") or ((feed.get("videos") or [{}])[0].get("video_id") or "")
    carousel = len(link.get("child_attachments") or [])
    if video_id:
        fmt = "Video"
    elif carousel > 1:
        fmt = "Carousel"
    elif feed.get("images") and len(feed["images"]) > 1:
        fmt = "Dynamic"
    else:
        fmt = "Image"
    return {"body": body, "title": title, "description": desc, "cta": cta, "url": url, "videoId": video_id,
            "format": fmt, "cards": carousel, "variants": {"bodies": bodies, "titles": titles}}


def video_info(api, video_ids):
    """video id -> {length, thumb, permalink}; batched so a big account costs a handful of calls."""
    out, ids = {}, sorted(set(i for i in video_ids if i))
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        try:
            res = api.get("", {"ids": ",".join(chunk), "fields": "length,picture,permalink_url,title"})
        except RuntimeError as e:
            print(f"! video details skipped for {len(chunk)} videos ({e})")
            continue
        for vid, v in res.items():
            out[vid] = {"length": _n(v.get("length")), "thumb": v.get("picture") or "",
                        "permalink": v.get("permalink_url") or ""}
    return out


def pull(client):
    creds = _creds()
    account = act_id(client, creds)
    api = Api(creds["token"])
    result_kind = client.get("meta_result", "purchase")
    print(f"Meta Ads: pulling {account} ({DATE_PRESET})...")
    info = api.get(account, {"fields": "name,currency,timezone_name,account_status"})
    ads = api.pages(f"{account}/ads", {"fields": AD_FIELDS, "limit": 100,
                                       "effective_status": json.dumps(["ACTIVE", "PAUSED", "ADSET_PAUSED",
                                                                       "CAMPAIGN_PAUSED", "ARCHIVED", "WITH_ISSUES"])})
    ins = api.pages(f"{account}/insights", {"level": "ad", "date_preset": DATE_PRESET, "fields": INSIGHT_FIELDS,
                                            "limit": 200})
    by_ad = {r["ad_id"]: parse_insights(r, result_kind) for r in ins if r.get("ad_id")}
    daily = api.pages(f"{account}/insights", {"level": "account", "date_preset": DATE_PRESET, "time_increment": 1,
                                              "fields": "spend,impressions,clicks,actions,action_values", "limit": 100})
    series = [{"date": d.get("date_start"), **{k: v for k, v in parse_insights(d, result_kind).items()
                                               if k in ("spend", "impr", "clicks", "results", "value")}} for d in daily]
    fields = {a["id"]: creative_fields(a.get("creative")) for a in ads}
    vids = video_info(api, [f["videoId"] for f in fields.values()])
    rows = []
    for a in ads:
        cr, f = a.get("creative") or {}, fields[a["id"]]
        v = vids.get(f["videoId"], {})
        rows.append({
            "id": a["id"], "name": a.get("name", ""), "status": a.get("effective_status") or a.get("status", ""),
            "created": a.get("created_time", ""),
            "campaignId": (a.get("campaign") or {}).get("id", ""), "campaign": (a.get("campaign") or {}).get("name", ""),
            "objective": (a.get("campaign") or {}).get("objective", ""),
            "adsetId": (a.get("adset") or {}).get("id", ""), "adset": (a.get("adset") or {}).get("name", ""),
            **f, "videoLength": v.get("length", 0), "thumbUrl": v.get("thumb") or cr.get("image_url") or cr.get("thumbnail_url") or "",
            "permalink": v.get("permalink") or cr.get("instagram_permalink_url") or "",
            "previewLink": a.get("preview_shareable_link", ""),
            "m": by_ad.get(a["id"]) or parse_insights({}, result_kind),
        })
    print(f"Meta Ads: {len(rows)} ads, {len(by_ad)} with spend, {api.requests} API calls")
    return {"version": CACHE_VERSION, "pulledAt": datetime.now().isoformat(timespec="seconds"),
            "account": {"id": account, "name": info.get("name", ""), "currency": info.get("currency", "USD"),
                        "timezone": info.get("timezone_name", ""), "resultKind": result_kind},
            "dateRange": DATE_PRESET.replace("_", " ").title(), "ads": rows, "daily": series}


# -------------------------------------------------------------------- cache
def load_or_pull(key, client):
    """Cached pull (META_CACHE_HOURS). A failed refresh falls back to the last good cache."""
    if sample_mode():
        return sample(client)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"meta_{key}.json"
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
    if cached and cached.get("version") != CACHE_VERSION:
        cached = None
    force = os.environ.get("META_FORCE", "") == "1"
    if cached and not force:
        age = (datetime.now() - datetime.fromisoformat(cached["pulledAt"])).total_seconds() / 3600
        if age < CACHE_HOURS:
            print(f"Meta Ads: using cache ({age:.1f}h old)")
            return cached
    try:
        data = pull(client)
    except Exception as e:
        if cached:
            print(f"! Meta Ads refresh failed ({e}); using the cache from {cached['pulledAt']}")
            return cached
        raise
    if data["ads"]:
        path.write_text(json.dumps(data), encoding="utf-8")
    return data


# --------------------------------------------------------------------- sample
def sample(client):
    """Deterministic made-up data so the layout can be tested without credentials. Never mix with real data."""
    import random
    rnd = random.Random(7)
    hooks = ["I stopped buying 5 different serums.", "Dermatologists won't tell you this about SPF.",
             "Your skin barrier is crying for help.", "Watch what happens after 14 days.",
             "New: the gentle cleanser sensitive skin deserves.", "Before you buy another moisturizer, read this."]
    ads = []
    for i in range(14):
        video = i % 3 != 2
        hook_rate = rnd.uniform(0.12, 0.42) if video else 0
        impr = rnd.randint(8000, 90000)
        spend = impr / 1000 * rnd.uniform(9, 22)
        plays = impr * hook_rate
        ctr = rnd.uniform(0.006, 0.024)
        clicks = impr * ctr
        results = clicks * rnd.uniform(0.01, 0.06)
        ret = [rnd.uniform(0.45, 0.8), rnd.uniform(0.3, 0.55), rnd.uniform(0.18, 0.4), rnd.uniform(0.08, 0.22), rnd.uniform(0.04, 0.15)]
        ret.sort(reverse=True)
        h = hooks[i % len(hooks)]
        ads.append({
            "id": f"2385000000{i:04d}", "name": f"SAMPLE {'UGC video' if video else 'Static'} {i + 1}",
            "status": "ACTIVE" if i % 5 else "PAUSED", "created": "2026-10-01T10:00:00+0000",
            "campaignId": "c1" if i < 8 else "c2", "campaign": "SAMPLE | Prospecting" if i < 8 else "SAMPLE | Retargeting",
            "objective": "OUTCOME_SALES", "adsetId": f"as{i % 3}", "adset": f"SAMPLE Ad set {i % 3 + 1}",
            "body": f"{h}\nSample body copy for layout testing only.", "title": "Shop now", "description": "",
            "cta": "SHOP_NOW", "url": "https://soteriskin.com", "videoId": f"v{i}" if video else "",
            "format": "Video" if video else "Image", "cards": 0, "variants": {"bodies": [], "titles": []},
            "videoLength": rnd.choice([12, 22, 31, 45]) if video else 0, "thumbUrl": "", "permalink": "", "previewLink": "",
            "m": {"spend": round(spend, 2), "impr": impr, "reach": int(impr * 0.7), "clicks": clicks,
                  "linkClicks": clicks * 0.8, "results": results, "value": results * rnd.uniform(30, 70),
                  "plays3s": plays, "p25": plays * ret[0], "p50": plays * ret[1], "p75": plays * ret[2],
                  "p95": plays * ret[3], "p100": plays * ret[4], "thruplay": plays * ret[1] * 0.9, "s30": plays * ret[3],
                  "avgWatch": rnd.uniform(3, 14) if video else 0, "addToCart": results * 2.4},
        })
    if not video_present(ads):
        raise RuntimeError("sample generation failed")
    daily = [{"date": f"2026-11-{d:02d}", "spend": 300 + rnd.random() * 120, "impr": 30000 + rnd.randint(0, 9000),
              "clicks": 500 + rnd.randint(0, 200), "results": 12 + rnd.randint(0, 9), "value": 600 + rnd.randint(0, 400)}
             for d in range(1, 16)]
    return {"version": CACHE_VERSION, "pulledAt": datetime.now().isoformat(timespec="seconds"), "sample": True,
            "account": {"id": "act_SAMPLE", "name": "SAMPLE ACCOUNT", "currency": "USD", "timezone": "", "resultKind": "purchase"},
            "dateRange": "Last 30D", "ads": ads, "daily": daily}


def video_present(ads):
    return any(a["format"] == "Video" for a in ads)
