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
DEADLINE = None  # set per run; AI calls must finish before it
NEWS_RATIO = float(os.environ.get("NEWS_RATIO", "0.45"))  # chance to try research news first
FRESH_RATIO = float(os.environ.get("FRESH_RATIO", "0.9"))  # otherwise: chance of a new topic post vs basic tip bank
SHOW_SOURCE = os.environ.get("SHOW_SOURCE", "true").lower() == "true"
FOOTER = os.environ.get("FOOTER", "Dr. Debela D.")
TIME_BUDGET = 100  # seconds; stay under the 120s maxDuration in vercel.json
REPEAT_DAYS = int(os.environ.get("REPEAT_DAYS", "60"))  # same tip / tip bank tip not again before this
TOPIC_COOLDOWN_DAYS = 60  # same topic not again before this (when enough other topics are left)
CATEGORY_GAP = 4          # the next post avoids the subject areas of the last 4 posts
MAX_TIMES = 2             # a tip is posted at most twice: the original + one repost
SIMILARITY = 0.3          # word overlap above this counts as a repeat of a recent post

FEEDS = [
    "https://www.sciencedaily.com/rss/health_medicine/dentistry.xml",
    "https://news.google.com/rss/search?q=dental+research+study&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=oral+health+study+researchers&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=periodontitis+OR+%22gum+disease%22+study&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=dentistry+new+technology+OR+innovation&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=oral+microbiome+research&hl=en-US&gl=US&ceid=US:en",
]
KEYWORDS = ["dental", "dentist", "teeth", "tooth", "oral", "gum", "cavity", "cavities",
            "floss", "fluoride", "enamel", "plaque", "mouth"]
MAX_NEWS_AGE_DAYS = 10

# Topics for new posts, grouped by subject area. Consecutive posts come from different areas,
# and a topic is not reused for TOPIC_COOLDOWN_DAYS. Add topics freely.
TOPIC_AREAS = {
    "mouth-body connection": [
        "gum disease and heart disease: what the link means", "gum disease and diabetes: a two-way relationship",
        "oral health and pregnancy outcomes", "oral bacteria and pneumonia in older adults",
        "periodontitis and inflammation in the whole body", "oral health and brain health research",
        "the mouth as an early warning sign of other diseases",
    ],
    "oral microbiome": [
        "the oral microbiome: friendly and harmful bacteria", "how plaque biofilm forms and why it is hard to remove",
        "how sugar feeds acid-producing bacteria", "saliva: the mouth's natural defence system",
        "probiotics and oral health: what research says so far", "why mouthwash is not a substitute for brushing",
    ],
    "how teeth work": [
        "enamel: the hardest substance in the body and why it cannot regrow",
        "demineralisation and remineralisation: the daily battle on your teeth",
        "dentin and why exposed roots become sensitive", "how a cavity develops step by step",
        "how fluoride works at the chemical level", "wisdom teeth: why many people have problems with them",
    ],
    "gum health": [
        "gingivitis vs periodontitis: the difference", "receding gums: causes and prevention",
        "bone loss around teeth: the silent process", "why bleeding gums are never normal",
        "smoking hides the signs of gum disease",
    ],
    "nutrition science": [
        "acid erosion from soft drinks, juices and sports drinks", "calcium, vitamin D and strong teeth",
        "hidden sugars in everyday foods", "snacking frequency and the acid clock in your mouth",
        "vitamin C and gum health", "Ethiopian foods and drinks and their effects on teeth",
    ],
    "lifestyle risks": [
        "khat chewing: effects on gums, teeth and the mouth", "tobacco and oral cancer risk",
        "alcohol and oral health", "stress, teeth grinding and jaw pain", "sleep, snoring and dry mouth",
        "energy drinks and tooth erosion",
    ],
    "oral cancer and serious signs": [
        "oral cancer: early warning signs adults should know", "HPV and throat and mouth cancers",
        "white and red patches in the mouth", "why a mouth ulcer lasting over two weeks needs checking",
    ],
    "modern dental treatments": [
        "dental implants: how they fuse with bone", "root canal treatment: myths vs reality",
        "how modern fillings are made", "crowns and bridges explained", "teeth whitening: safe vs harmful methods",
        "digital X-rays and 3D scans in dentistry", "lasers in modern dentistry",
    ],
    "dental research and future": [
        "research on regrowing tooth enamel", "stem cells from teeth in medical research",
        "vaccines against tooth decay: where research stands", "artificial intelligence in dental diagnosis",
        "saliva tests that may detect diseases", "3D printing in dentistry",
    ],
    "jaw, bite and alignment": [
        "jaw joint (TMJ) problems explained", "crooked teeth and bite problems in adults",
        "braces and clear aligners: how teeth move through bone", "mouth breathing and its effects",
    ],
    "life stages": [
        "oral health changes after age 50", "menopause and oral health", "tooth loss and nutrition in older adults",
        "teenagers: sugary drinks, sports and oral health", "why baby teeth matter for adult teeth",
    ],
    "myths and facts": [
        "myth: brushing harder cleans better", "myth: no pain means no dental problem",
        "myth: tooth loss is a normal part of ageing", "myth: sugar-free means safe for teeth",
        "charcoal toothpaste: what the evidence says", "traditional chewing sticks (mefakia): evidence and limits",
    ],
    "oral hygiene science": [
        "why two minutes of brushing matters", "electric vs manual toothbrushes: what studies show",
        "cleaning between teeth: floss, brushes and what works best", "the tongue and bad breath bacteria",
        "how often to replace a toothbrush and why",
    ],
}
TOPICS = [(area, t) for area, topics in TOPIC_AREAS.items() for t in topics]

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
WRITER_SYSTEM = """You write dental health posts for a public Telegram community run by a dentist in Ethiopia.
Readers are mostly adults who want real understanding, not only simple tips. Each post is in English, Afaan Oromoo and Amharic.

Style:
- Give high-level understanding: explain what happens in the mouth or body and why, in plain words,
  then what it means for the reader. Teach something most people do not already know.
- For research news: say what researchers found or are studying, why it matters, and that it is research
  (not yet everyday treatment) where that is true.
- Do not repeat basic "brush twice a day" advice unless the topic is exactly that. Do not aim posts at children
  unless the topic is about children.
- Each language version: 4 to 6 sentences, about 500 to 700 characters, clear and warm. One emoji at the start is fine.

Safety rules (very important, posts are published without human review):
- Stay with mainstream, evidence-based dental science. Clearly say when something is early research.
- NEVER name medicines, antibiotics or painkillers, and NEVER give doses.
- NEVER diagnose, and never suggest home treatment for pain, swelling, bleeding or infection: say to see a dentist.
- Do not state numbers, statistics or study results unless they appear in the source text.
- Do not copy sentences from the source; write original text.

Language rules:
- "om": natural everyday Afaan Oromoo in Qubee (Latin script).
- "am": natural everyday Amharic in Ge'ez (Fidel) script.
- All three versions must say the same thing.

If the source cannot be turned into a useful post for ordinary adults
(e.g. business news, company stocks, lawsuits, product ads, local clinic announcements), set "skip" to true.

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

def ask_claude(system, user, max_tokens=2000):
    """Send a prompt through OpenRouter and return the text reply."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    timeout = 40 if DEADLINE is None else min(40, DEADLINE - time.time())
    if timeout < 5:
        raise RuntimeError("out of time for another AI call")
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
        timeout=timeout,
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
    try:
        return json.loads(text[text.find("{"):text.rfind("}") + 1])
    except ValueError:
        raise ValueError(f"model reply was not JSON: {text[:150]!r}")


def write_tip(source_text):
    data = parse_json(ask_claude(WRITER_SYSTEM, source_text, max_tokens=4000))
    if data.get("skip") or not all(str(data.get(k, "")).strip() for k in ("en", "om", "am")):
        return None
    return data


def is_ethiopic(c):
    return "ሀ" <= c <= "᎟" or "ⶀ" <= c <= "⷟" or "꬀" <= c <= "꬯"


def wrong_script(tip):
    """Catch letters from other scripts (e.g. a Bengali word inside the Amharic) that the AI check can miss."""
    for key, allowed in (("en", lambda c: c < "ɐ"), ("om", lambda c: c < "ɐ"),
                         ("am", lambda c: is_ethiopic(c) or c < "ɐ")):
        bad = [c for c in tip[key] if c.isalpha() and not allowed(c)]
        if bad:
            return f"{key} has letters from another script: {''.join(bad[:10])!r}"
    return None


def passes_check(tip, log):
    problem = wrong_script(tip)
    if problem:
        log.append("rejected: " + problem)
        return False
    payload = json.dumps({k: tip[k] for k in ("en", "om", "am")}, ensure_ascii=False)
    result = parse_json(ask_claude(CHECKER_SYSTEM, payload, max_tokens=800))
    if not result.get("ok"):
        log.append("rejected by safety check: " + str(result.get("reason")))
        return False
    return True


# ---------------- Repeat control ----------------
def now_iso():
    return datetime.now(timezone.utc).isoformat()


def days_since(iso):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 86400


def load_posts(r):
    """Original AI posts, newest first, with how often and when each was last shown (reposts included)."""
    meta = r.hgetall("repost_log") or {}
    out = []
    for raw in (r.lrange("posts", 0, 499) or []):
        try:
            p = json.loads(raw)
            m = json.loads(meta[p["ts"]]) if p["ts"] in meta else {}
        except Exception:
            continue
        p["count"], p["last"] = m.get("count", 1), m.get("last", p["ts"])
        out.append(p)
    return out


COMMON = {"your", "teeth", "tooth", "that", "this", "with", "dentist", "dental", "mouth", "they", "from", "have",
          "will", "what", "when", "more", "also", "about", "which", "their", "health", "oral", "help", "helps",
          "can't", "don't", "it's", "these", "there", "because", "keep", "even", "like", "make", "very"}


def words(text):
    return {w for w in re.findall(r"[a-z']+", text.lower()) if len(w) > 3 and w not in COMMON}


def too_similar(tip, posts, log):
    new = words(tip["en"])
    for p in posts:
        old = words(p["en"])
        if days_since(p["last"]) < REPEAT_DAYS and new and old and len(new & old) / len(new | old) > SIMILARITY:
            log.append("rejected: too similar to a recent post")
            return True
    return False


def accept(tip, posts, log):
    return not too_similar(tip, posts, log) and passes_check(tip, log)


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


def tip_from_news(r, posts, start, log):
    for item in fresh_news(r, log)[:3]:
        if time.time() - start > 50:  # leave time for the other sources
            break
        r.sadd("seen_news", item["link"])
        try:
            tip = write_tip("Recent dental research or news. Explain it for ordinary adults.\n\n"
                            f"Headline: {item['title']}\n\nSummary: {item['summary'][:2000]}")
            if tip and accept(tip, posts, log):
                tip["source"], tip["area"] = item["link"], "research news"
                return tip
            log.append("news item skipped: " + item["title"][:80])
        except Exception as e:
            log.append(f"news item failed: {e}")
    return None


# ---------------- Tip bank ----------------
def tip_from_bank(r, posts, start, log):
    last = r.hgetall("tip_last") or {}
    ready = [t for t in TIPS if t[0] not in last or days_since(last[t[0]]) >= REPEAT_DAYS]
    if not ready:
        log.append(f"tip bank: every tip was used in the last {REPEAT_DAYS} days")
        return None
    random.shuffle(ready)
    for tip_id, text in ready[:3]:
        if time.time() - start > TIME_BUDGET - 15:
            break
        try:
            tip = write_tip("Evergreen dental tip approved by the dentist. Keep the meaning exactly, "
                            f"expand it warmly and translate it:\n\n{text}")
            if tip and accept(tip, posts, log):
                tip["source"], tip["area"] = "tip-bank:" + tip_id, "basics"
                return tip
        except Exception as e:
            log.append(f"tip {tip_id} failed: {e}")
    return None


def pick_topics(r, posts, n=3):
    """Up to n topics, each from a different subject area, avoiding recent areas and topics used lately."""
    last = r.hgetall("topic_last") or {}
    recent_areas = {p.get("area") for p in posts[:CATEGORY_GAP]}
    fresh = [t for t in TOPICS if t[1] not in last or days_since(last[t[1]]) >= TOPIC_COOLDOWN_DAYS]
    pool = [t for t in fresh if t[0] not in recent_areas] or fresh \
        or sorted(TOPICS, key=lambda t: last.get(t[1], ""))[:10]  # all used lately: the longest-unused ones
    random.shuffle(pool)
    picked, areas = [], set()
    for area, topic in pool:
        if area not in areas:
            picked.append((area, topic))
            areas.add(area)
        if len(picked) == n:
            break
    return picked


def tip_from_topic(r, posts, start, log):
    avoid = "\n".join("- " + p["en"][:150] for p in posts[:60]) or "(none yet)"
    for area, topic in pick_topics(r, posts):
        if time.time() - start > TIME_BUDGET - 15:
            break
        try:
            tip = write_tip(
                f"Write a NEW post about: {topic} (subject area: {area}).\n\n"
                "It must be about a completely different subject from these earlier posts, "
                f"not a rewording of any of them:\n{avoid}"
            )
            if tip and accept(tip, posts, log):
                tip["source"], tip["area"] = "topic:" + topic, area
                return tip
        except Exception as e:
            log.append(f"topic '{topic}' failed: {e}")
    return None


# ---------------- Fallback (no AI needed, e.g. OpenRouter out of credits) ----------------
def tip_from_archive(posts, log):
    """Repost an earlier AI tip that already passed the safety check.
    Prefer tips shown fewer than MAX_TIMES and not in the last REPEAT_DAYS; otherwise the one shown longest ago."""
    ready = [p for p in posts if p["count"] < MAX_TIMES and days_since(p["last"]) >= REPEAT_DAYS]
    if ready:
        post = random.choice(ready)
        log.append("AI unavailable: reposting an earlier tip")
    else:
        older = [p for p in posts if days_since(p["last"]) >= 3]  # never one shown in the last 3 days
        if not older:
            return None
        post = min(older, key=lambda p: (p["count"], p["last"]))
        log.append(f"AI unavailable and no tip past {REPEAT_DAYS} days: reposting the oldest one")
    return {"en": post["en"], "om": post["om"], "am": post["am"], "source": "archive:" + post["ts"],
            "count": post["count"]}


def tip_from_bank_english(r, log):
    """Last resort: the dentist-approved tip bank tip used longest ago, English only."""
    last = r.hgetall("tip_last") or {}
    tip_id, text = min(TIPS, key=lambda t: last.get(t[0], ""))
    log.append("AI unavailable and no earlier post old enough to repost: posting an English tip bank tip")
    return {"en": text, "source": "tip-bank-en:" + tip_id}


# ---------------- Telegram ----------------
def format_message(tip):
    e = html.escape
    parts = ["🦷 <b>Dental Tip · የጥርስ ጤና ምክር · Gorsa Fayyaa Ilkaanii</b>"]
    for key, label in (("en", "English"), ("am", "አማርኛ"), ("om", "Afaan Oromoo")):
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
def remember(r, tip):
    """Record a sent post so repeat control knows what was shown and when."""
    ts, source = now_iso(), tip["source"]
    if source.startswith("archive:"):  # reposts aren't stored again, only counted
        r.hset("repost_log", source[len("archive:"):], json.dumps({"count": tip["count"] + 1, "last": ts}))
        return
    if source.startswith("tip-bank"):
        r.hset("tip_last", source.split(":", 1)[1], ts)
    if source.startswith("tip-bank-en:"):  # English-only last resort: not kept for reposting
        return
    if source.startswith("topic:"):
        r.hset("topic_last", source[len("topic:"):], ts)
    r.lpush("posts", json.dumps({"ts": ts, "source": source, "area": tip.get("area"),
                                 "en": tip["en"], "om": tip["om"], "am": tip["am"]}, ensure_ascii=False))
    r.ltrim("posts", 0, 499)


def run_once(dry_run=False):
    global DEADLINE
    start, log = time.time(), []
    DEADLINE = start + TIME_BUDGET
    r = get_redis()
    posts = load_posts(r)
    tip = None
    if random.random() < NEWS_RATIO:
        tip = tip_from_news(r, posts, start, log)
    if not tip:
        order = [tip_from_topic, tip_from_bank] if random.random() < FRESH_RATIO else [tip_from_bank, tip_from_topic]
        for source_fn in order:
            tip = source_fn(r, posts, start, log)
            if tip:
                break
    if not tip:
        tip = tip_from_archive(posts, log) or tip_from_bank_english(r, log)
    message = format_message(tip)
    if dry_run:
        return {"posted": False, "dry_run": True, "message": message, "source": tip["source"], "log": log}
    send_telegram(message)
    remember(r, tip)
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
        reply = ask_claude("Reply with the single word OK.", "ping", max_tokens=200)
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
