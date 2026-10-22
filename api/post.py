"""
Dental Tips Auto-Poster for Telegram, Vercel version.
English + Afaan Oromoo + Amharic, fully automatic.

Vercel Cron calls GET /api/post on schedule (see vercel.json).
Manual use in a browser:
  /api/post?key=YOUR_CRON_SECRET&dry=1   -> preview a tip (nothing is posted)
  /api/post?key=YOUR_CRON_SECRET         -> post one tip now
  /api/post?key=YOUR_CRON_SECRET&check=1 -> test all connections (nothing is posted)
"""

import html
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import feedparser
import requests
from upstash_redis import Redis

# ---------------- Settings (Vercel environment variables) ----------------
# Any model from https://openrouter.ai/models (copy its exact ID).
# Several IDs separated by commas = backups: OpenRouter uses the first one that works.
MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
MODELS = [m.strip() for m in MODEL.split(",") if m.strip()]
LAST_MODEL = None
NEWS_RATIO = float(os.environ.get("NEWS_RATIO", "0.3"))   # chance to try news first
FRESH_RATIO = float(os.environ.get("FRESH_RATIO", "0.6"))  # otherwise: chance of a brand-new topic tip vs tip bank
SHOW_SOURCE = os.environ.get("SHOW_SOURCE", "true").lower() == "true"
FOOTER = os.environ.get("FOOTER", "")
TIME_BUDGET = 50  # seconds; stay under Vercel's 60s function limit

FEEDS = [
    "https://news.google.com/rss/search?q=dental+health&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=oral+health+tips&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=tooth+decay+prevention&hl=en-US&gl=US&ceid=US:en",
    "https://www.sciencedaily.com/rss/health_medicine/dentistry.xml",
]
KEYWORDS = ["dental", "dentist", "teeth", "tooth", "oral", "gum", "cavity", "cavities",
            "floss", "fluoride", "enamel", "plaque", "mouth"]
MAX_NEWS_AGE_DAYS = 10

# Topics for brand-new tips (keeps hourly posts varied)
TOPICS = [
    "correct brushing technique", "how long to brush", "choosing a toothbrush", "fluoride toothpaste",
    "flossing or cleaning between teeth", "cleaning the tongue", "bad breath causes and prevention",
    "sugar and snacking habits", "sweet drinks, soda and juice", "drinking water for teeth",
    "healthy foods for strong teeth", "sticky sweets and candy", "tea and coffee stains",
    "baby's first teeth", "baby bottles at bedtime", "helping children brush", "children's first dental visit",
    "teething babies", "protecting teenagers' teeth", "teeth during pregnancy", "oral health for older people",
    "caring for dentures", "dental check-ups", "bleeding gums", "gum disease warning signs",
    "toothache means see a dentist", "sensitive teeth", "smoking and oral health", "khat chewing and teeth",
    "dry mouth", "mouth sores that don't heal", "diabetes and gum health", "sports and protecting teeth",
    "not using teeth as tools", "teeth grinding", "traditional chewing sticks (mefakia)",
    "keeping toothbrushes clean", "brushing at night before sleep", "school children and oral health",
    "myths about teeth", "a knocked-out tooth is an emergency", "braces and crooked teeth",
]

# Evergreen tip bank (id, text). Add more freely; ids must be unique.
TIPS = [
    ('brush-twice', 'Brush your teeth twice a day, in the morning and before bed, for about two minutes each time.'),
    ('fluoride-paste', 'Use a toothpaste that contains fluoride. Fluoride makes tooth enamel stronger and helps prevent cavities.'),
    ('spit-dont-rinse', "After brushing at night, spit out the toothpaste but don't rinse heavily with water, so the fluoride keeps protecting your teeth."),
    ('floss-daily', 'Clean between your teeth once a day with floss or interdental brushes. A toothbrush cannot reach these spaces.'),
    ('change-brush', 'Replace your toothbrush every 3 months, or sooner if the bristles are bent or worn.'),
    ('soft-brush', 'Use a soft-bristled toothbrush and gentle circular movements. Hard scrubbing can damage gums and enamel.'),
    ('tongue', 'Gently clean your tongue when you brush. It helps remove bacteria and keeps your breath fresh.'),
    ('sugar-frequency', 'How often you eat sugar matters more than how much. Try to keep sweets and sugary drinks to mealtimes instead of snacking all day.'),
    ('soft-drinks', 'Soft drinks and sweetened juices attack your teeth with sugar and acid. Water is the best drink for your teeth.'),
    ('water-after-meals', 'Drinking or rinsing with water after meals helps wash away food and sugar from your teeth.'),
    ('checkup', 'Visit a dentist for a check-up at least once a year, even when nothing hurts. Problems found early are easier and cheaper to treat.'),
    ('bleeding-gums', 'Gums that bleed when you brush are a sign of gum disease. Keep brushing gently and see a dentist for a check.'),
    ('toothache-dentist', "A toothache or swelling in the face or gums needs a dentist's attention. Don't wait for the pain to go away on its own."),
    ('baby-first-tooth', "Start cleaning your baby's teeth as soon as the first tooth appears, using a soft small brush and a tiny smear of fluoride toothpaste."),
    ('baby-bottle', "Don't put a baby to sleep with a bottle of milk, juice or sweet drink. It can cause serious tooth decay in baby teeth."),
    ('child-supervise', 'Help or supervise children while they brush until about age 7 or 8. Young children cannot yet brush thoroughly on their own.'),
    ('baby-teeth-matter', 'Baby teeth matter. They help children eat, speak, and keep space for adult teeth, so protect them from decay.'),
    ('tobacco', 'Smoking and chewing tobacco stain teeth, cause gum disease and raise the risk of mouth cancer. Quitting protects your mouth and your whole body.'),
    ('khat', 'Chewing khat can dry the mouth, stain teeth and harm the gums. If you chew, rinse with water and keep up good brushing, and visit a dentist regularly.'),
    ('dry-mouth', 'Saliva protects your teeth. If your mouth often feels dry, drink water regularly and mention it to your dentist or doctor.'),
    ('teeth-not-tools', "Don't use your teeth to open bottles or bite hard objects. This can crack or break teeth."),
    ('mouth-sore', "A sore, white or red patch in the mouth that doesn't heal within two weeks should be checked by a dentist."),
    ('pregnancy', 'During pregnancy, gums can become more sensitive and bleed more easily. Keep brushing and flossing, and it is safe to visit the dentist.'),
    ('diabetes', 'People with diabetes have a higher risk of gum disease. Good daily cleaning and regular dental visits are especially important.'),
    ('healthy-food', 'Fruits, vegetables, milk and other healthy foods help keep teeth and gums strong. Limit sticky, sugary snacks.'),
    ('mefakia', 'Traditional chewing sticks (mefakia) can help clean teeth, but use them gently and keep using fluoride toothpaste too.'),
]

# ---------------- Storage (Upstash Redis) ----------------
def get_redis():
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
    if not url or not token:
        raise RuntimeError("Redis not configured: add Upstash Redis to the Vercel project.")
    return Redis(url=url, token=token)


# ---------------- Claude ----------------
WRITER_SYSTEM = """You write short dental health tips for a public Telegram community group run by a dentist in Ethiopia.
Readers are ordinary people of all ages. Each tip is posted in English, Afaan Oromoo and Amharic.

Safety rules (very important, posts are published without human review):
- Give only general, widely accepted oral-health advice: brushing, flossing, fluoride toothpaste, diet and sugar,
  water, children's teeth, tobacco and khat effects, regular check-ups, warning signs that need a dentist.
- NEVER name medicines, antibiotics or painkillers, and NEVER give doses.
- NEVER diagnose, and never suggest home treatment for pain, swelling, bleeding or infection: say to see a dentist.
- Do not state numbers, statistics or study results unless they appear in the source text.
- Do not copy sentences from the source; write an original practical tip.
- Each language version: 2 to 4 short sentences, under 350 characters, warm and simple. One emoji at the start is fine.

Language rules:
- "om": natural everyday Afaan Oromoo in Qubee (Latin script).
- "am": natural everyday Amharic in Ge'ez (Fidel) script.
- All three versions must say the same thing.

If the source cannot be turned into a useful practical tip for ordinary people
(e.g. business news, company stocks, lawsuits, product ads, research too technical), set "skip" to true.

Reply with ONLY a JSON object, no other text:
{"skip": false, "en": "...", "om": "...", "am": "..."}"""

CHECKER_SYSTEM = """You are a strict dental-health and language reviewer. A tip will be published automatically
to a public group in English, Afaan Oromoo (Qubee script) and Amharic (Ge'ez script) with no human review.

Reject (ok=false) if ANY of these is true:
- Medically wrong, misleading or potentially harmful advice
- Mentions medicine names, antibiotics, painkillers, or doses
- Diagnoses, or suggests home treatment for pain/swelling/bleeding/infection instead of seeing a dentist
- Invented statistics or claims
- The three versions do not mean the same thing
- Any version is garbled, in the wrong script, or in the wrong language
- Anything offensive, political, or commercial

Reply with ONLY a JSON object: {"ok": true, "reason": ""}"""

def ask_claude(system, user, max_tokens=1200):
    """Send a prompt through OpenRouter and return the text reply."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": "Dental Tips Bot",
        },
        json={
            **({"models": MODELS} if len(MODELS) > 1 else {"model": MODELS[0]}),
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=25,
    )
    if not resp.ok:
        raise RuntimeError(f"OpenRouter error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if "choices" not in data:
        raise RuntimeError(f"OpenRouter error: {str(data)[:300]}")
    global LAST_MODEL
    LAST_MODEL = data.get("model")  # which backup model actually answered
    return data["choices"][0]["message"]["content"] or ""


def parse_json(text):
    text = re.sub(r"```(?:json)?", "", text).strip()
    return json.loads(text[text.find("{"):text.rfind("}") + 1])


def write_tip(source_text):
    data = parse_json(ask_claude(WRITER_SYSTEM, source_text))
    if data.get("skip") or not all(str(data.get(k, "")).strip() for k in ("en", "om", "am")):
        return None
    return data


def passes_check(tip, log):
    payload = json.dumps({k: tip[k] for k in ("en", "om", "am")}, ensure_ascii=False)
    result = parse_json(ask_claude(CHECKER_SYSTEM, payload, max_tokens=300))
    if not result.get("ok"):
        log.append("rejected by safety check: " + str(result.get("reason")))
        return False
    return True


# ---------------- News ----------------
def clean_html(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def fresh_news(r, log):
    items = []
    cutoff = datetime.utcnow() - timedelta(days=MAX_NEWS_AGE_DAYS)
    for url in FEEDS:
        try:
            resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 dental-tips-bot"})
            feed = feedparser.parse(resp.content)
        except Exception as e:
            log.append(f"feed failed {url}: {e}")
            continue
        for e in feed.entries:
            link = e.get("link")
            if not link:
                continue
            published = e.get("published_parsed") or e.get("updated_parsed")
            if published and datetime(*published[:6]) < cutoff:
                continue
            title, summary = clean_html(e.get("title", "")), clean_html(e.get("summary", ""))
            if not any(k in (title + " " + summary).lower() for k in KEYWORDS):
                continue
            items.append({"link": link, "title": title, "summary": summary,
                          "date": datetime(*published[:6]) if published else datetime.min})
    items.sort(key=lambda x: x["date"], reverse=True)
    return [i for i in items[:30] if not r.sismember("seen_news", i["link"])]


def tip_from_news(r, start, log):
    for item in fresh_news(r, log)[:3]:
        if time.time() - start > 25:  # leave time for the tip bank fallback
            break
        r.sadd("seen_news", item["link"])
        try:
            tip = write_tip(f"News headline: {item['title']}\n\nNews summary: {item['summary'][:2000]}")
            if tip and passes_check(tip, log):
                tip["source"] = item["link"]
                return tip
            log.append("news item skipped: " + item["title"][:80])
        except Exception as e:
            log.append(f"news item failed: {e}")
    return None


# ---------------- Tip bank ----------------
def tip_from_bank(r, start, log):
    used = set(r.smembers("used_tips") or [])
    unused = [t for t in TIPS if t[0] not in used]
    if not unused:
        r.delete("used_tips")
        unused = list(TIPS)
    random.shuffle(unused)
    for tip_id, text in unused[:3]:
        if time.time() - start > TIME_BUDGET - 15:
            break
        try:
            tip = write_tip("Evergreen dental tip approved by the dentist. Keep the meaning exactly, "
                            f"rewrite it warmly and translate it:\n\n{text}")
            if tip and passes_check(tip, log):
                r.sadd("used_tips", tip_id)
                tip["source"] = "tip-bank:" + tip_id
                return tip
        except Exception as e:
            log.append(f"tip {tip_id} failed: {e}")
    return None


def recent_posts(r, n=24):
    out = []
    for raw in (r.lrange("posts", 0, n - 1) or []):
        try:
            out.append(json.loads(raw)["en"])
        except Exception:
            pass
    return out


def tip_from_topic(r, start, log):
    recent = recent_posts(r)
    avoid = "\n".join("- " + t for t in recent) or "(none yet)"
    for topic in random.sample(TOPICS, 3):
        if time.time() - start > TIME_BUDGET - 15:
            break
        try:
            tip = write_tip(
                f"Write a NEW practical tip about this topic: {topic}.\n\n"
                f"It must be clearly different from these recently posted tips:\n{avoid}"
            )
            if tip and passes_check(tip, log):
                tip["source"] = "topic:" + topic
                return tip
        except Exception as e:
            log.append(f"topic '{topic}' failed: {e}")
    return None


# ---------------- Fallback (no AI needed, e.g. OpenRouter out of credits) ----------------
def tip_from_archive(r, log):
    """Repost an earlier AI tip that already passed the safety check, skipping the most recent ones."""
    posts = r.lrange("posts", 0, 499) or []
    older = posts[24:] or posts[1:] or posts
    reposted = set(r.smembers("reposted") or [])
    choices = [p for p in older if json.loads(p)["ts"] not in reposted]
    if not choices and older:
        r.delete("reposted")
        choices = older
    if not choices:
        return None
    post = json.loads(random.choice(choices))
    r.sadd("reposted", post["ts"])
    log.append("AI unavailable: reposting an earlier tip")
    return {"en": post["en"], "om": post["om"], "am": post["am"], "source": "archive:" + post["ts"]}


def tip_from_bank_english(log):
    """Last resort: a dentist-approved tip bank tip, English only."""
    tip_id, text = random.choice(TIPS)
    log.append("AI unavailable and no earlier posts: posting an English tip bank tip")
    return {"en": text, "source": "tip-bank-en:" + tip_id}


# ---------------- Telegram ----------------
def format_message(tip):
    e = html.escape
    parts = ["🦷 <b>Dental Tip · Gorsa Fayyaa Ilkaanii · የጥርስ ጤና ምክር</b>"]
    for key, label in (("en", "English"), ("om", "Afaan Oromoo"), ("am", "አማርኛ")):
        if tip.get(key):
            parts.append(f"<b>{label}</b>\n{e(tip[key])}")
    if SHOW_SOURCE and str(tip.get("source", "")).startswith("http"):
        parts.append(f'📰 <a href="{e(tip["source"])}">Source</a>')
    if FOOTER:
        parts.append(e(FOOTER))
    return "\n\n".join(parts)


def send_telegram(text):
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                               "disable_web_page_preview": True}, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Telegram error {resp.status_code}: {resp.text}")


# ---------------- Main job ----------------
def run_once(dry_run=False):
    start, log = time.time(), []
    r = get_redis()
    tip = None
    if random.random() < NEWS_RATIO:
        tip = tip_from_news(r, start, log)
    if not tip:
        order = [tip_from_topic, tip_from_bank] if random.random() < FRESH_RATIO else [tip_from_bank, tip_from_topic]
        for source_fn in order:
            tip = source_fn(r, start, log)
            if tip:
                break
    fallback = not tip
    if fallback:
        tip = tip_from_archive(r, log) or tip_from_bank_english(log)
    message = format_message(tip)
    if dry_run:
        return {"posted": False, "dry_run": True, "message": message, "source": tip["source"], "log": log}
    send_telegram(message)
    if not fallback:  # only new AI tips go in the archive, so reposts don't pile up
        r.lpush("posts", json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "source": tip["source"],
                                     "en": tip["en"], "om": tip["om"], "am": tip["am"]}, ensure_ascii=False))
        r.ltrim("posts", 0, 499)
    return {"posted": True, "source": tip["source"], "seconds": round(time.time() - start, 1), "log": log}


def health_check():
    """Verify every connection without posting anything."""
    results = {}
    try:
        get_redis().ping()
        results["redis"] = "OK"
    except Exception as e:
        results["redis"] = f"FAIL: {e}"

    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    try:
        me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10).json()
        if not me.get("ok"):
            raise RuntimeError(me.get("description", "invalid TELEGRAM_BOT_TOKEN"))
        results["telegram_bot"] = "OK (@" + me["result"]["username"] + ")"
        member = requests.get(f"https://api.telegram.org/bot{token}/getChatMember",
                              params={"chat_id": chat_id, "user_id": me["result"]["id"]}, timeout=10).json()
        if not member.get("ok"):
            raise RuntimeError("group: " + member.get("description", "wrong TELEGRAM_CHAT_ID"))
        status = member["result"]["status"]
        results["telegram_group"] = "OK (bot is admin)" if status in ("administrator", "creator") \
            else f"WARNING: bot status is '{status}', make it an admin of the group"
    except Exception as e:
        results.setdefault("telegram_bot", f"FAIL: {e}")
        results["telegram_group"] = f"FAIL: {e}"

    try:
        reply = ask_claude("Reply with the single word OK.", "ping", max_tokens=10)
        results["openrouter"] = f"OK (model {LAST_MODEL or MODEL} replied: {reply.strip()[:20]})"
    except Exception as e:
        results["openrouter"] = f"FAIL: {e}"

    results["cron_secret"] = "OK"  # if you can see this page, the key works
    results["all_ok"] = all(str(v).startswith("OK") for v in results.values() if v is not True)
    return results


class handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        secret = os.environ.get("CRON_SECRET", "")
        query = parse_qs(urlparse(self.path).query)
        authorized = bool(secret) and (
            self.headers.get("authorization", "") == f"Bearer {secret}"
            or query.get("key", [""])[0] == secret
        )
        if not authorized:
            return self._send(401, {"error": "unauthorized"})
        try:
            if query.get("check", ["0"])[0] == "1":
                return self._send(200, health_check())
            self._send(200, run_once(dry_run=query.get("dry", ["0"])[0] == "1"))
        except Exception as e:
            self._send(500, {"error": str(e)})
