#!/usr/bin/env python3
"""
codex reset tracker
-------------------
Polls public X/Twitter accounts through Nitter RSS mirrors (no X API, no keys),
keyword-prefilters new posts, asks a FREE OpenRouter model whether the post is
really about the topic we care about (usage-limit resets), and pings Telegram.

Stdlib only. Designed to run on a GitHub Actions cron.
"""

import argparse
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import ssl

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "state.json")

# Nitter blocks generic/curl user agents but serves RSS clients happily.
RSS_UA = "Tiny Tiny RSS/24.02 (https://tt-rss.org/)"
MAX_AGE_HOURS = 48          # never alert on anything older than this
SEEN_CAP = 400              # per-handle ring buffer of post ids
BLIND_AFTER = 12            # consecutive all-mirrors-failed runs before warning
BLIND_REPEAT = 144          # ...and how often to re-warn after that


def _ssl_ctx():
    """Normal verified TLS. Set TRACKER_INSECURE_SSL=1 only for local boxes with a
    broken/intercepted CA store — never in CI."""
    if os.environ.get("TRACKER_INSECURE_SSL") == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl_ctx()


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- http helpers

def http_get(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read().decode("utf-8", "replace")


def http_post_json(url, payload, headers=None, timeout=90):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ------------------------------------------------------------------ rss layer

ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
TAG_RE = re.compile(r"<[^>]+>")


def _field(block, tag):
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S)
    if not m:
        return ""
    val = m.group(1).strip()
    if val.startswith("<![CDATA["):
        val = val[9:-3] if val.endswith("]]>") else val[9:]
    return val


def _clean(text):
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = TAG_RE.sub("", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def parse_feed(xml, handle):
    posts = []
    for block in ITEM_RE.findall(xml):
        pid = _field(block, "guid")
        if not pid:
            continue
        pid = _clean(pid)
        link = _clean(_field(block, "link"))
        title = _clean(_field(block, "title"))
        body = _clean(_field(block, "description"))
        text = body if len(body) >= len(title) else title
        if not text:
            continue

        # nitter link -> canonical x.com link
        m = re.search(r"/([^/]+)/status/(\d+)", link)
        author = m.group(1) if m else handle
        url = f"https://x.com/{author}/status/{m.group(2)}" if m else link

        when = None
        raw_date = _clean(_field(block, "pubDate"))
        if raw_date:
            try:
                when = parsedate_to_datetime(raw_date)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except Exception:
                when = None

        kind = "repost" if title.startswith("RT by @") else "post"
        posts.append({
            "id": pid, "handle": handle, "author": author, "text": text,
            "url": url, "when": when, "kind": kind,
        })
    return posts


TG_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def fetch_telegram_channel(channel):
    """Public Telegram channels expose a keyless HTML preview at t.me/s/<name>.
    @codexreset detects resets off OpenAI's own API, so it usually beats the
    X announcement by a wide margin."""
    page = http_get(f"https://t.me/s/{channel}", {"User-Agent": TG_UA})
    blocks = re.split(r'(?=data-post="' + re.escape(channel) + r'/\d+")', page)
    posts = []
    for b in blocks[1:]:
        m = re.match(r'data-post="([^"]+)"', b)
        if not m:
            continue
        pid = m.group(1)
        tm = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', b, re.S)
        if not tm:
            continue
        text = _clean(tm.group(1))
        if not text:
            continue
        when = None
        dt = re.findall(r'<time[^>]+datetime="([^"]+)"', b)
        if dt:
            try:
                when = datetime.fromisoformat(dt[-1])
            except Exception:
                pass
        posts.append({
            "id": f"tg:{pid}", "handle": f"tg/{channel}", "author": channel,
            "text": text, "url": f"https://t.me/{pid}", "when": when, "kind": "channel",
        })
    posts.reverse()  # newest first, matching the RSS sources
    return posts


def fetch_handle(handle, instances):
    """Try mirrors until one returns a usable feed. Returns (posts, ok)."""
    pool = instances[:]
    random.shuffle(pool)
    for base in pool:
        url = f"{base.rstrip('/')}/{handle}/rss"
        try:
            xml = http_get(url, {"User-Agent": RSS_UA,
                                 "Accept": "application/rss+xml, application/xml"})
        except Exception as e:
            log(f"  {base} -> {type(e).__name__}: {e}")
            continue
        posts = parse_feed(xml, handle)
        if posts:
            log(f"  {base} -> {len(posts)} posts")
            return posts, True
        log(f"  {base} -> empty feed")
    return [], False


# ------------------------------------------------------------------ filtering

def build_prefilter(words):
    return re.compile("|".join(rf"\b{w}\b" for w in words), re.I)


# High-confidence fallback used when the LLM is unavailable.
STRONG = [
    re.compile(r"\breset\w*\b.{0,80}\b(limit|usage|credit|quota|cap|rate)", re.I | re.S),
    re.compile(r"\b(limit|usage|credit|quota|cap|rate)\w*\b.{0,80}\breset", re.I | re.S),
    re.compile(r"\b(extra|more|free|bonus|double)\s+(usage|credits?|limits?|capacity)", re.I),
    re.compile(r"\blimits?\b.{0,40}\b(are back|back to normal|restored|refreshed|raised|increased|bumped)", re.I | re.S),
]


def strong_hit(text):
    return any(p.search(text) for p in STRONG)


# ----------------------------------------------------------------- openrouter

SYSTEM = (
    "You are a strict binary classifier for a monitoring bot. "
    "You answer only with JSON. No prose, no markdown fences."
)

TEMPLATE = """We monitor X/Twitter posts for ONE topic:

TOPIC: {topic}

WHAT COUNTS: {desc}

WHAT DOES NOT COUNT: general model/product news, benchmarks, hiring, philosophy,
unrelated outages, jokes, or vague hype with no mention of usage limits/credits.

Classify each post below. A post counts (hit=true) only if it announces, confirms,
or credibly reports an actual change/reset to usage limits, rate limits, quotas or
credits. Speculation and questions do NOT count.

POSTS:
{posts}

Reply with ONLY a JSON array, one object per post, in order:
[{{"i": 0, "hit": true, "why": "<max 15 words>"}}]"""


def classify(posts, cfg, api_key):
    """Returns dict id -> (hit, why, source). Falls back to regex on failure."""
    listing = "\n\n".join(
        f"[{i}] @{p['author']} ({p['kind']}): {p['text'][:700]}"
        for i, p in enumerate(posts)
    )
    prompt = TEMPLATE.format(topic=cfg["topic"], desc=cfg["topic_description"], posts=listing)

    if api_key:
        for model in cfg["models"]:
            # reasoning models (gpt-5-nano et al) burn the token budget on thinking
            # unless effort is pinned low; models that don't know the field 400 on it.
            for body_extra in ({"reasoning": {"effort": "low"}}, {}):
              try:
                payload = {
                    "model": model,
                    "temperature": 0,
                    "max_tokens": 1500,
                    "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                }
                payload.update(body_extra)
                resp = http_post_json(
                    "https://openrouter.ai/api/v1/chat/completions",
                    payload,
                    {
                        "Authorization": f"Bearer {api_key}",
                        "HTTP-Referer": "https://github.com/codex-reset-tracker",
                        "X-Title": "codex reset tracker",
                    },
                )
                content = resp["choices"][0]["message"].get("content") or ""
                if not content.strip():
                    raise ValueError(f"empty content (finish={resp['choices'][0].get('finish_reason')})")
                m = re.search(r"\[.*\]", content, re.S)
                verdicts = json.loads(m.group(0) if m else content)
                out = {}
                for v in verdicts:
                    i = int(v.get("i", -1))
                    if 0 <= i < len(posts):
                        out[posts[i]["id"]] = (
                            bool(v.get("hit")), str(v.get("why", ""))[:160], model,
                        )
                if out:
                    cost = (resp.get("usage") or {}).get("cost", 0)
                    log(f"  classified {len(out)} posts with {model} (${cost:.5f})")
                    return out
                raise ValueError("model returned no usable verdicts")
              except Exception as e:
                body = ""
                if isinstance(e, urllib.error.HTTPError):
                    try:
                        body = e.read().decode()[:200]
                    except Exception:
                        pass
                log(f"  model {model} failed: {type(e).__name__}: {e} {body}")
                time.sleep(1)
    else:
        log("  no OPENROUTER_API_KEY set")

    log("  falling back to strict regex matching")
    return {p["id"]: (strong_hit(p["text"]), "strict keyword match (LLM unavailable)", "regex")
            for p in posts}


# ------------------------------------------------------------------- telegram

def tg(method, params, token):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
        return json.loads(r.read().decode())


def send(text, token, chat_id, preview=True):
    try:
        tg("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false" if preview else "true",
        }, token)
        return True
    except Exception as e:
        body = ""
        if isinstance(e, urllib.error.HTTPError):
            try:
                body = e.read().decode()[:300]
            except Exception:
                pass
        log(f"  telegram send failed: {e} {body}")
        return False


def esc(s):
    return html.escape(s, quote=False)


def alert_text(post, why, model):
    stamp = post["when"].astimezone(timezone.utc).strftime("%d %b %H:%M UTC") if post["when"] else "just now"
    if post["kind"] == "channel":
        tag = "📡"
    elif post["kind"] == "repost":
        tag = "🔁 repost by"
    else:
        tag = "🐦"
    body = post["text"]
    if len(body) > 900:
        body = body[:900] + "…"
    return (
        f"🚨 <b>RESET ALERT</b> — {tag} {esc(post['handle'] if post['kind'] == 'channel' else '@' + post['handle'])}\n"
        f"<i>{stamp}</i>\n\n"
        f"{esc(body)}\n\n"
        f"🧠 <i>{esc(why)}</i>\n"
        f"{post['url']}"
    )


# ----------------------------------------------------------------------- state

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="classify but never send Telegram")
    ap.add_argument("--test-alert", action="store_true", help="send one test message and exit")
    ap.add_argument("--selftest", action="store_true",
                    help="run the classifier against known-good/known-bad sample posts")
    ap.add_argument("--recheck", type=int, default=0,
                    help="re-scan the N most recent posts per handle even if already seen")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"seen": {}, "seeded": False, "last_heartbeat": None})
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()

    if args.selftest:
        samples = [
            ("HIT", "sam altman", "ok we hear you \u2014 we're going to do a reset on codex usage limits tonight, everyone gets a fresh weekly allowance"),
            ("HIT", "tibor blaho", "OpenAI just reset Codex rate limits for all Plus and Pro users, new quota is live in the dashboard"),
            ("MISS", "sam altman", "GPT-5.6 is now rolling out to everyone. it's a big jump on coding benchmarks and we are excited"),
            ("MISS", "tibor blaho", "ChatGPT Android beta shows a new 'Sunspot' refresh for temporary chat"),
            ("MISS", "sam altman", "does anyone else think we should reset how we think about AGI timelines?"),
        ]
        posts = [{"id": str(i), "author": a, "kind": "post", "text": t, "handle": a,
                  "url": "", "when": None} for i, (_, a, t) in enumerate(samples)]
        verdicts = classify(posts, cfg, or_key)
        ok = True
        for i, (want, _, t) in enumerate(samples):
            hit, why, model = verdicts.get(str(i), (False, "", "?"))
            got = "HIT" if hit else "MISS"
            flag = "\u2705" if got == want else "\u274c"
            if got != want:
                ok = False
            print(f"{flag} want={want:4} got={got:4} [{model}] {t[:64]}...  ({why})")
        print("selftest", "PASSED" if ok else "FAILED")
        return 0 if ok else 1

    if not token or not chat_id:
        log("FATAL: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing")
        return 2

    if args.test_alert:
        ok = send("✅ <b>codex reset tracker</b> — test alert. Wiring works.", token, chat_id)
        return 0 if ok else 1

    prefilter = build_prefilter(cfg["prefilter"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    seen = state.setdefault("seen", {})
    streaks = state.setdefault("fail_streak", {})
    first_run = not state.get("seeded")

    fresh, scanned = [], 0
    for handle in cfg["handles"]:
        if handle.startswith("tg/"):
            log(f"fetching telegram channel {handle}")
            try:
                posts = fetch_telegram_channel(handle[3:])
                ok = bool(posts)
                log(f"  t.me/s/{handle[3:]} -> {len(posts)} messages")
            except Exception as e:
                posts, ok = [], False
                log(f"  t.me/s/{handle[3:]} -> {type(e).__name__}: {e}")
        else:
            log(f"fetching @{handle}")
            posts, ok = fetch_handle(handle, cfg["nitter_instances"])
        if not ok:
            streaks[handle] = streaks.get(handle, 0) + 1
            log(f"  !! every mirror failed for @{handle} (streak {streaks[handle]}) — state untouched")
            # ~1h of consecutive failures = we are blind; warn once, then every ~12h
            if streaks[handle] == BLIND_AFTER or (
                streaks[handle] > BLIND_AFTER and streaks[handle] % BLIND_REPEAT == 0
            ):
                send(f"\u26a0\ufe0f <b>tracker is blind on @{esc(handle)}</b>\n"
                     f"Every mirror has failed {streaks[handle]} runs in a row. "
                     f"Nitter instances may be down \u2014 check the pool in config.json.",
                     token, chat_id, preview=False)
            continue
        if streaks.get(handle, 0) >= BLIND_AFTER:
            send(f"\u2705 back online for @{esc(handle)} after {streaks[handle]} failed runs.",
                 token, chat_id, preview=False)
        streaks[handle] = 0
        scanned += len(posts)
        known = set(seen.get(handle, []))
        recheck = {p["id"] for p in posts[:args.recheck]} if args.recheck else set()
        for p in posts:
            if p["id"] in known and p["id"] not in recheck:
                continue
            if p["when"] and p["when"] < cutoff:
                continue
            fresh.append(p)
        # remember everything the mirror returned, newest first, capped
        merged = [p["id"] for p in posts] + [i for i in seen.get(handle, []) if i not in {p["id"] for p in posts}]
        seen[handle] = merged[:SEEN_CAP]

    log(f"scanned {scanned} posts, {len(fresh)} new")

    if first_run and scanned == 0:
        log("first run but every mirror failed — not seeding, will retry next cycle")
        return 1

    if first_run:
        state["seeded"] = True
        state["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        send(f"👀 <b>codex reset tracker armed</b>\nWatching: "
             f"{', '.join('@' + h for h in cfg['handles'])}\n"
             f"Baseline of {scanned} recent posts saved — you'll only get pinged on new ones.",
             token, chat_id, preview=False)
        log("first run: baseline saved, no alerts sent")
        return 0

    if cfg.get("prefilter_enabled", True):
        candidates = [p for p in fresh if prefilter.search(p["text"])]
        log(f"{len(candidates)} of {len(fresh)} passed the keyword prefilter")
    else:
        candidates = fresh
        log(f"prefilter off - judging all {len(candidates)} new posts")

    alerts = 0
    if candidates:
        verdicts = classify(candidates[:20], cfg, or_key)
        for p in candidates:
            if p["id"] in verdicts:
                hit, why, model = verdicts[p["id"]]
            else:
                # model skipped this one - never drop a post silently
                hit, why, model = strong_hit(p["text"]), "keyword match (model returned no verdict)", "regex"
                if hit:
                    log(f"  no verdict for {p['url']}, regex says hit")
            if not hit:
                continue
            if alerts >= cfg.get("max_alerts_per_run", 6):
                log("  hit max_alerts_per_run, stopping")
                break
            log(f"  ALERT {p['url']} :: {why}")
            if args.dry_run:
                print(alert_text(p, why, model))
                alerts += 1
            elif send(alert_text(p, why, model), token, chat_id):
                alerts += 1

    # dead-man's switch: if these stop arriving, the tracker is down
    hb_hours = cfg.get("heartbeat_hours", 24)
    if hb_hours:
        last = state.get("last_heartbeat")
        due = True
        if last:
            try:
                due = datetime.fromisoformat(last) < datetime.now(timezone.utc) - timedelta(hours=hb_hours)
            except Exception:
                due = True
        if due and not args.dry_run:
            send(f"💤 tracker alive — {scanned} posts scanned this cycle, nothing worth waking you for.",
                 token, chat_id, preview=False)
            state["last_heartbeat"] = datetime.now(timezone.utc).isoformat()

    if not args.dry_run:
        save_state(state)
    log(f"done. {alerts} alert(s) sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
