"""
Dental Tips Auto-Poster for Telegram, Vercel version.
English + Afaan Oromoo + Amharic, fully automatic.

Vercel Cron calls GET /api/post on schedule (see vercel.json).
Manual use in a browser:
  /api/post?key=YOUR_CRON_SECRET&dry=1   -> preview a tip (nothing is posted)
  /api/post?key=YOUR_CRON_SECRET         -> post one tip now
  /api/post?key=YOUR_CRON_SECRET&check=1 -> test all connections (nothing is posted)
  /api/post?key=YOUR_CRON_SECRET&quiz=1&dry=1 -> preview a quiz (quizzes post on QUIZ_DAYS automatically)
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
NEWS_RATIO = float(os.environ.get("NEWS_RATIO", "0.25"))  # chance to try dental research news first
FRESH_RATIO = float(os.environ.get("FRESH_RATIO", "0.9"))  # otherwise: chance of a new topic post vs basic tip bank
SHOW_SOURCE = os.environ.get("SHOW_SOURCE", "true").lower() == "true"
FOOTER = os.environ.get("FOOTER", "Dr. Debela D.")
TIME_BUDGET = 100  # seconds; stay under the 120s maxDuration in vercel.json
REPEAT_DAYS = int(os.environ.get("REPEAT_DAYS", "60"))  # same tip / tip bank tip not again before this
TOPIC_COOLDOWN_DAYS = 60  # same topic not again before this (when enough other topics are left)
CATEGORY_GAP = 4          # the next post avoids the subject areas of the last 4 posts
MAX_TIMES = 2             # a tip is posted at most twice: the original + one repost
SIMILARITY = 0.3          # word overlap above this counts as a repeat of a recent post
READY_TARGET = 5          # spare posts written ahead of time, used when the AI is down
IMAGE_RATIO = float(os.environ.get("IMAGE_RATIO", "0.5"))  # share of tip posts with a Pexels photo
QUIZ_DAYS = {1, 4}        # Tuesday and Friday (Monday = 0): the first run of the day posts a quiz
ADDIS = timezone(timedelta(hours=3))

# Safe photo searches the AI may choose from (Pexels). Keeping a fixed list avoids odd or graphic images.
IMAGE_QUERIES = [
    "toothbrush", "toothpaste on toothbrush", "brushing teeth", "dental floss", "smiling african woman",
    "smiling african man", "african family smiling", "smiling child", "dentist examining patient",
    "dental clinic", "dental chair", "dental x-ray", "dental tools", "dental implant model", "teeth model",
    "coffee ceremony", "cup of tea", "sugarcane", "sweets and candy", "soft drinks", "fresh vegetables",
    "fruits", "glass of water", "milk", "honey", "bread", "elderly african man smiling", "pregnant woman",
    "cigarette smoke", "sports", "toothache", "mouthwash", "chewing stick",
]

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
# and a topic is not reused for TOPIC_COOLDOWN_DAYS. Dental only, Ethiopian context. Add topics freely.
TOPIC_AREAS = {
    "Ethiopian food and drink": [
        "sugar in buna (coffee ceremony): three rounds of sweet coffee and your teeth",
        "sweet shai (tea) through the day and tooth decay",
        "chewing sugarcane: sugar, hard fibres and teeth",
        "kolo, dabo kolo and sticky snacks between teeth",
        "honey and tej: natural sugar still causes cavities",
        "soft drinks and juice sold in shops: acid and sugar on teeth",
        "teff, milk, ayib and vegetables: foods that help teeth and gums",
        "coffee and tea stains on teeth and how to reduce them",
    ],
    "holidays and fasting": [
        "holiday sweets and drinks at Genna, Timket, Meskel and Eid: protecting teeth",
        "fasting seasons (Tsom) and Ramadan: keeping teeth and breath healthy",
        "wedding and holiday feasts: snacking all day and the acid attack on teeth",
        "breaking the fast: what to eat and drink first for your teeth",
    ],
    "fluoride and water in Ethiopia": [
        "dental fluorosis in the Rift Valley: brown spots from high-fluoride water",
        "why fluoride toothpaste still matters, and why not to swallow it",
        "choosing a toothpaste in Ethiopian shops: check for fluoride",
        "safe drinking water and the teeth of growing children in high-fluoride areas",
    ],
    "traditional practices": [
        "mefakia (chewing stick): how to use it well and its limits",
        "cutting out a baby's tooth buds (so-called 'false teeth') is harmful: why to avoid it",
        "home remedies on a painful tooth: why they can make things worse",
        "charcoal and ash for cleaning teeth: why they damage enamel",
        "salt and water rinses: what they can and cannot do",
    ],
    "khat, tobacco and shisha": [
        "khat chewing: staining, dry mouth and gum damage",
        "khat and sweet drinks together: double damage to teeth",
        "cigarettes and shisha: stains, bad breath and gum disease",
        "chewing tobacco and white patches in the mouth",
        "quitting khat or tobacco: how the mouth recovers",
    ],
    "brushing and cleaning well": [
        "correct brushing technique: small circles along the gumline",
        "why two minutes of brushing matters",
        "brushing at night before sleep: the most important brush of the day",
        "cleaning between teeth without floss: what works",
        "cleaning the tongue for fresher breath",
        "choosing and caring for a toothbrush, and when to replace it",
    ],
    "gum health": [
        "bleeding gums are not normal: what they mean",
        "gingivitis vs periodontitis: from swollen gums to loose teeth",
        "receding gums and long-looking teeth",
        "loose teeth in adults: causes and when to see a dentist",
        "tartar (hard deposits) and why only a dentist can remove it",
    ],
    "tooth decay and pain": [
        "how a cavity develops step by step",
        "early signs of tooth decay you can see",
        "toothache: why waiting makes treatment harder and costlier",
        "swelling of the face or gums from a tooth: an emergency",
        "sensitive teeth to cold or hot drinks",
    ],
    "seeing a dentist in Ethiopia": [
        "why a check-up before pain saves money",
        "what happens at a dental check-up and cleaning",
        "filling vs pulling a tooth: why saving the tooth is better",
        "root canal treatment: myths vs reality",
        "after a tooth extraction: caring for the wound",
        "replacing missing teeth: dentures, bridges and implants explained",
    ],
    "family and life stages": [
        "first teeth: start cleaning from the first tooth",
        "baby bottles and sweet drinks at night",
        "school children, sweets from shops and tooth decay",
        "pregnancy and bleeding gums",
        "teeth and gums after age 50",
        "caring for dentures",
    ],
    "breath and mouth care": [
        "bad breath: real causes and real solutions",
        "dry mouth and why saliva protects teeth",
        "mouth sores that do not heal in two weeks",
        "teeth grinding and jaw pain from stress",
    ],
    "injuries and emergencies": [
        "a knocked-out tooth: what to do in the first hour",
        "broken or chipped teeth from falls and sports",
        "using teeth to open bottles and crack hard things",
    ],
    "new dental research": [
        "new research on repairing early tooth decay without drilling",
        "research on the bacteria that cause tooth decay",
        "new materials for fillings and how long they last",
        "research on how sugar frequency affects decay",
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
Readers are ordinary people in Ethiopia, mostly adults. Each post is in English, Afaan Oromoo and Amharic.

Content:
- DENTAL ONLY: teeth, gums, tongue, mouth and dental care. Do not write about the heart, brain, diabetes, general
  diseases or general health; if a source is mainly about those, set "skip" to true.
- Ethiopian context: use local life where it fits naturally (buna and shai with sugar, sugarcane, kolo, injera and
  teff, khat, mefakia, holidays and fasting seasons, high-fluoride water in the Rift Valley, limited access to
  dentists and the cost of treatment). Advice must be practical and affordable for people in Ethiopia.
- Every post is a GOOD, PRACTICAL TIP: briefly explain why it matters (what happens to the teeth or gums),
  then say clearly what to do. End with when to see a dentist only where it fits.
- For research news: say in plain words what was found, what it means for the reader's teeth, and that it is
  still research where that is true.
- Fluoride protects teeth mainly from toothpaste on the tooth surface; never suggest swallowing fluoride.
- Do not aim posts at children unless the topic is about children.
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

If the source cannot be turned into a useful dental post for people in Ethiopia (e.g. not mainly about teeth
or gums, business news, company stocks, lawsuits, product ads, clinic announcements abroad), set "skip" to true.

Also choose "image": the ONE phrase from this list that best fits the post as a photo: """ + ", ".join(IMAGE_QUERIES) + """

Reply with ONLY a JSON object, no other text:
{"skip": false, "en": "...", "om": "...", "am": "...", "image": "..."}"""

CHECKER_SYSTEM = """You are a strict dental-health and language reviewer. A tip will be published automatically
to a public group in English, Afaan Oromoo (Qubee script) and Amharic (Ge'ez script) with no human review.

Reject (ok=false) if ANY of these is true:
- Not mainly about teeth, gums or the mouth (e.g. mostly about the heart, brain or general health)
- Not a clear, practical tip or useful explanation for ordinary people in Ethiopia
- Medically wrong, misleading or potentially harmful advice (e.g. suggesting to swallow fluoride)
- Mentions medicine names, antibiotics, painkillers, or doses
- Diagnoses, or suggests home treatment for pain/swelling/bleeding/infection instead of seeing a dentist
- Invented statistics or claims
- The three versions do not mean the same thing
- Any version is garbled, in the wrong script, or in the wrong language
- Anything offensive, political, or commercial

Reply with ONLY a JSON object: {"ok": true, "reason": ""}"""

QUIZ_SYSTEM = """You write dental quizzes for a public Telegram community run by a dentist in Ethiopia.
Readers are ordinary people in Ethiopia. The quiz is a Telegram quiz poll in English, Amharic and Afaan Oromoo.

Rules:
- DENTAL ONLY, Ethiopian context where it fits (buna, shai, sugarcane, kolo, khat, mefakia, fasting, Rift Valley water).
- A common myth-or-fact or a practical question with ONE clearly correct answer, based on mainstream dental science.
- Same safety rules as always: no medicine names or doses, no diagnosis, no home treatment for pain or infection.
- Strict length limits (Telegram): each question line at most 90 characters; 2 to 4 options; each option text
  in each language at most 28 characters; "explanation" in English at most 180 characters.
- "answer_en", "answer_am", "answer_om": the full answer post published later, 4 to 6 sentences each,
  about 500 to 700 characters: restate the question, give the correct answer, explain why, and the practical tip.
- "am" in Ge'ez (Fidel) script, "om" in Afaan Oromoo Qubee (Latin script). All languages say the same thing.

Reply with ONLY a JSON object, no other text:
{"question_en": "...", "question_am": "...", "question_om": "...",
 "options": [{"en": "...", "am": "...", "om": "..."}, ...], "correct": 0,
 "explanation": "...", "answer_en": "...", "answer_am": "...", "answer_om": "..."}"""

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
        return json.loads(text[text.find("{"):text.rfind("}") + 1], strict=False)  # allow line breaks in text
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
            tip = write_tip("Recent dental research or news. Turn it into a practical dental post for people in Ethiopia.\n\n"
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
            "count": post["count"], "image": post.get("image")}


def tip_from_ready(r, posts, log, take=True):
    """A spare post written on an earlier run (never posted yet). take=False only peeks (for previews)."""
    for raw in (r.lrange("ready", 0, READY_TARGET * 2) or []):
        tip = json.loads(raw)
        if take:
            r.lrem("ready", 1, raw)
        if not too_similar(tip, posts, log):
            log.append("AI unavailable: posting a spare post written earlier")
            return tip
    return None


def refill_ready(r, start, log):
    """After posting, use leftover time to write one spare post for when the AI is down."""
    if (r.llen("ready") or 0) >= READY_TARGET or time.time() - start > 45:
        return
    posts = load_posts(r)
    posts = [{"en": json.loads(x)["en"], "last": now_iso(), "area": json.loads(x).get("area")}
             for x in (r.lrange("ready", 0, -1) or [])] + posts  # avoid repeating spares too
    tip = tip_from_topic(r, posts, start, log)
    if tip:
        r.rpush("ready", json.dumps(tip, ensure_ascii=False))
        r.hset("topic_last", tip["source"][len("topic:"):], now_iso())  # reserve the topic
        log.append("wrote a spare post for later")


def tip_from_bank_english(r, log):
    """Last resort: the dentist-approved tip bank tip used longest ago, English only."""
    last = r.hgetall("tip_last") or {}
    tip_id, text = min(TIPS, key=lambda t: last.get(t[0], ""))
    log.append("AI unavailable and no earlier post old enough to repost: posting an English tip bank tip")
    return {"en": text, "source": "tip-bank-en:" + tip_id}


# ---------------- Telegram ----------------
def format_message(tip):
    e = html.escape
    parts = [tip.get("title") or "🦷 <b>Dental Tip · የጥርስ ጤና ምክር · Gorsa Fayyaa Ilkaanii</b>"]
    for key, label in (("en", "English"), ("am", "አማርኛ"), ("om", "Afaan Oromoo")):
        if tip.get(key):
            parts.append(f"<b>{label}</b>\n{e(tip[key])}")
    if SHOW_SOURCE and str(tip.get("source", "")).startswith("http"):
        parts.append(f'📰 <a href="{e(tip["source"])}">Source</a>')
    if FOOTER:
        parts.append(e(FOOTER))
    if tip.get("credit"):
        parts.append(tip["credit"])
    return "\n\n".join(parts)


def telegram(method, payload):
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    resp = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                         json={"chat_id": chat_id, **payload}, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Telegram error {resp.status_code}: {resp.text}")
    return resp.json()


def send_telegram(text, image_url=None):
    """Send a post; with image_url the picture is shown large above the text (link preview,
    so long text still fits in one message)."""
    preview = ({"url": image_url, "prefer_large_media": True, "show_above_text": True} if image_url
               else {"is_disabled": True})
    try:
        telegram("sendMessage", {"text": text, "parse_mode": "HTML", "link_preview_options": preview})
    except RuntimeError:
        if not image_url:
            raise
        telegram("sendMessage", {"text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}})


def send_quiz(quiz):
    options = [" · ".join(o[k] for k in ("en", "am", "om")) for o in quiz["options"]]
    question = "🧩 " + "\n".join(quiz[k] for k in ("question_en", "question_am", "question_om"))
    telegram("sendPoll", {"question": question[:300], "options": [{"text": o[:100]} for o in options],
                          "type": "quiz", "correct_option_id": quiz["correct"], "is_anonymous": True,
                          "explanation": quiz.get("explanation", "")[:200]})


# ---------------- Pictures (Unsplash or Pixabay, whichever key is set) ----------------
def search_photos(query, log):
    """Photos for a query as (id, url, credit_html) from Unsplash or Pixabay."""
    e = html.escape
    try:
        if os.environ.get("UNSPLASH_ACCESS_KEY"):
            resp = requests.get("https://api.unsplash.com/search/photos",
                                headers={"Authorization": "Client-ID " + os.environ["UNSPLASH_ACCESS_KEY"]},
                                params={"query": query, "per_page": 30, "orientation": "landscape",
                                        "content_filter": "high"}, timeout=10)
            utm = "?utm_source=dental_tips_bot&utm_medium=referral"
            return [("u" + p["id"], p["urls"]["regular"],
                     f'📷 Photo: <a href="{e(p["user"]["links"]["html"] + utm)}">{e(p["user"]["name"])}</a>'
                     f' / <a href="https://unsplash.com/{utm}">Unsplash</a>', p["links"]["download_location"])
                    for p in (resp.json().get("results", []) if resp.ok else [])]
        if os.environ.get("PIXABAY_API_KEY"):
            resp = requests.get("https://pixabay.com/api/",
                                params={"key": os.environ["PIXABAY_API_KEY"], "q": query, "image_type": "photo",
                                        "orientation": "horizontal", "safesearch": "true", "per_page": 30},
                                timeout=10)
            return [("p" + str(p["id"]), p["largeImageURL"], "📷 Image: Pixabay", None)
                    for p in (resp.json().get("hits", []) if resp.ok else [])]
    except Exception as ex:
        log.append(f"photo search failed: {ex}")
    return []


def stock_image(r, query, log):
    """(url, credit) of a photo for the query that this bot has not used before, or (None, None)."""
    if query not in IMAGE_QUERIES:
        return None, None
    photos = search_photos(query, log)
    random.shuffle(photos)
    for photo_id, url, credit, download_location in photos:
        if not r.sismember("used_images", photo_id):
            r.sadd("used_images", photo_id)
            if download_location:  # Unsplash asks apps to report each photo they use
                try:
                    requests.get(download_location, timeout=5,
                                 headers={"Authorization": "Client-ID " + os.environ["UNSPLASH_ACCESS_KEY"]})
                except Exception:
                    pass
            return url, credit
    if photos:
        log.append(f"no unused photo for '{query}'")
    return None, None


def pick_image(r, tip, log):
    """News posts show the article's own preview; about IMAGE_RATIO of other posts get a stock photo.
    Returns (url, credit)."""
    source = str(tip.get("source", ""))
    if source.startswith("http"):
        return source, None
    if tip.get("image") and random.random() < IMAGE_RATIO:
        return stock_image(r, tip["image"], log)
    return None, None


# ---------------- Quiz ----------------
def write_quiz(r, posts, start, log):
    """A checked quiz on a topic from a fresh subject area, or None."""
    avoid = "\n".join("- " + p["en"][:150] for p in posts[:60]) or "(none yet)"
    for area, topic in pick_topics(r, posts, n=2):
        if time.time() - start > TIME_BUDGET - 30:
            break
        try:
            quiz = parse_json(ask_claude(QUIZ_SYSTEM, f"Write a quiz about: {topic} (subject area: {area}).\n\n"
                                         f"It must be about a different subject from these earlier posts:\n{avoid}",
                                         max_tokens=4000))
            problem = quiz_problem(quiz)
            if problem:
                log.append("quiz rejected: " + problem)
                continue
            answer = {"en": quiz["answer_en"], "am": quiz["answer_am"], "om": quiz["answer_om"]}
            if accept(answer, posts, log):
                quiz.update(source="quiz:" + topic, area=area)
                return quiz
        except Exception as e:
            log.append(f"quiz '{topic}' failed: {e}")
    return None


def quiz_problem(quiz):
    """Check Telegram's limits and the scripts; returns what is wrong, or None."""
    try:
        options, correct = quiz["options"], int(quiz["correct"])
        lines = [quiz[k] for k in ("question_en", "question_am", "question_om")]
        if not 2 <= len(options) <= 4 or not 0 <= correct < len(options):
            return "bad options"
        if len("🧩 " + "\n".join(lines)) > 300 or any(len(" · ".join(o[k] for k in ("en", "am", "om"))) > 100
                                                     for o in options):
            return "too long for Telegram"
        texts = {k: " ".join([quiz["question_" + k], quiz["answer_" + k]] + [o[k] for o in options])
                 for k in ("en", "am", "om")}
    except (KeyError, TypeError, ValueError):
        return "missing fields"
    return wrong_script(texts)


def quiz_due(r):
    today = datetime.now(ADDIS)
    return today.weekday() in QUIZ_DAYS and r.get("quiz_day") != today.date().isoformat()


def answer_post(quiz):
    return {"title": "🧩 <b>Quiz Answer · የጥያቄው መልስ · Deebii Gaaffii</b>",
            "en": quiz["answer_en"], "am": quiz["answer_am"], "om": quiz["answer_om"],
            "source": "quiz-answer:" + quiz["source"][len("quiz:"):], "area": quiz.get("area")}


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
    r.lpush("posts", json.dumps({"ts": ts, "source": source, "area": tip.get("area"), "image": tip.get("image"),
                                 "en": tip["en"], "om": tip["om"], "am": tip["am"]}, ensure_ascii=False))
    r.ltrim("posts", 0, 499)


def run_quiz(r, posts, start, log, dry_run):
    """Post a quiz; its full answer is posted by the next run. Returns the result, or None if no quiz was ready."""
    quiz = write_quiz(r, posts, start, log)
    if not quiz:
        return None
    if dry_run:
        return {"posted": False, "dry_run": True, "quiz": quiz, "log": log}
    send_quiz(quiz)
    r.set("pending_quiz", json.dumps(quiz, ensure_ascii=False))
    r.set("quiz_day", datetime.now(ADDIS).date().isoformat())
    r.hset("topic_last", quiz["source"][len("quiz:"):], now_iso())
    return {"posted": True, "source": quiz["source"], "seconds": round(time.time() - start, 1), "log": log}


def run_once(dry_run=False, force_quiz=False):
    global DEADLINE
    start, log = time.time(), []
    DEADLINE = start + TIME_BUDGET
    r = get_redis()
    posts = load_posts(r)
    pending = r.get("pending_quiz")
    if pending and not force_quiz:  # explain the last quiz before anything else
        tip = answer_post(json.loads(pending))
        message = format_message(tip)
        if dry_run:
            return {"posted": False, "dry_run": True, "message": message, "source": tip["source"], "log": log}
        send_telegram(message)
        r.delete("pending_quiz")
        remember(r, tip)
        return {"posted": True, "source": tip["source"], "seconds": round(time.time() - start, 1), "log": log}
    if force_quiz or quiz_due(r):
        result = run_quiz(r, posts, start, log, dry_run)
        if result:
            return result
        log.append("no quiz passed the checks: posting a normal tip instead")
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
        tip = (tip_from_ready(r, posts, log, take=not dry_run) or tip_from_archive(posts, log)
               or tip_from_bank_english(r, log))
    if dry_run:
        return {"posted": False, "dry_run": True, "message": format_message(tip), "source": tip["source"],
                "image": tip.get("image") and f"(photo search: {tip['image']})", "log": log}
    image, tip["credit"] = pick_image(r, tip, log)
    send_telegram(format_message(tip), image)
    remember(r, tip)
    refill_ready(r, start, log)
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

    if not (os.environ.get("UNSPLASH_ACCESS_KEY") or os.environ.get("PIXABAY_API_KEY")):
        results["photos"] = "WARNING: no UNSPLASH_ACCESS_KEY or PIXABAY_API_KEY set, tip posts will have no photos"
    else:
        log = []
        found = search_photos("toothbrush", log)
        results["photos"] = f"OK ({len(found)} photos found)" if found else f"FAIL: photo search found nothing {log}"

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
            self._send(200, run_once(dry_run=query.get("dry", ["0"])[0] == "1",
                                     force_quiz=query.get("quiz", ["0"])[0] == "1"))
        except Exception as e:
            self._send(500, {"error": str(e)})
