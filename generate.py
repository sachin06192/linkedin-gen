#!/usr/bin/env python3
"""
LinkedIn Post Generator — Two-Phase Pipeline
=============================================
Phase 1: python generate.py --research-only
  → Fetches trends, enriches with article content + HN comments, saves research JSON

Phase 2: Claude Code session reads research JSON and generates posts with deep research

Phase 3: python generate.py --images-only --input output/batch_YYYY-MM-DD.md
  → Searches/generates images for finalized posts
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, date
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

# ─── Config ───────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
IDEAS_PATH = ROOT / "ideas.txt"


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_ideas():
    """Load user ideas from ideas.txt, skipping comments and blank lines."""
    if not IDEAS_PATH.exists():
        return []
    ideas = []
    with open(IDEAS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cleaned = re.sub(r"^\d+\.\s*", "", line)
            if cleaned:
                ideas.append(cleaned)
    return ideas


# ─── Trend Fetching ──────────────────────────────────────────────────────────

def fetch_hackernews(limit=30, comments_per_story=5):
    """Fetch top stories from Hacker News with top comments."""
    print("  [HN] Fetching top stories + comments...")
    try:
        resp = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10
        )
        resp.raise_for_status()
        story_ids = resp.json()[:limit]

        stories = []

        def _get(sid):
            r = requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=10
            )
            r.raise_for_status()
            return r.json()

        def _get_comment(cid):
            """Fetch a single HN comment."""
            try:
                r = requests.get(
                    f"https://hacker-news.firebaseio.com/v0/item/{cid}.json", timeout=5
                )
                r.raise_for_status()
                item = r.json()
                if item and item.get("text") and not item.get("deleted"):
                    # Clean HTML from comment text
                    text = BeautifulSoup(item["text"], "lxml").get_text(separator=" ")
                    return text[:1000]
            except Exception:
                pass
            return None

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_get, sid): sid for sid in story_ids}
            for fut in as_completed(futures):
                try:
                    item = fut.result()
                    if item and item.get("title"):
                        story = {
                            "title": item["title"],
                            "url": item.get("url", ""),
                            "score": item.get("score", 0),
                            "source": "Hacker News",
                            "hn_id": item.get("id"),
                            "hn_comment_ids": item.get("kids", [])[:comments_per_story],
                        }
                        stories.append(story)
                except Exception:
                    pass

        # Fetch top comments for all stories in parallel
        all_comment_ids = []
        comment_map = {}  # cid -> story index
        for i, s in enumerate(stories):
            for cid in s.get("hn_comment_ids", []):
                all_comment_ids.append(cid)
                comment_map[cid] = i

        if all_comment_ids:
            comments_by_story = {i: [] for i in range(len(stories))}
            with ThreadPoolExecutor(max_workers=15) as pool:
                cfutures = {pool.submit(_get_comment, cid): cid for cid in all_comment_ids}
                for fut in as_completed(cfutures):
                    cid = cfutures[fut]
                    text = fut.result()
                    if text:
                        idx = comment_map[cid]
                        comments_by_story[idx].append(text)

            for i, s in enumerate(stories):
                s["hn_comments"] = comments_by_story.get(i, [])
                # Clean up temp fields
                s.pop("hn_comment_ids", None)

        stories.sort(key=lambda x: x["score"], reverse=True)
        total_comments = sum(len(s.get("hn_comments", [])) for s in stories)
        print(f"  [HN] Got {len(stories)} stories, {total_comments} comments")
        return stories
    except Exception as e:
        print(f"  [HN] Failed: {e}")
        return []


def fetch_reddit(subreddits, limit=15):
    """Fetch hot posts from Reddit subreddits with self-text."""
    print(f"  [Reddit] Fetching from r/{', r/'.join(subreddits)}...")
    posts = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; linkedin-gen/1.0)"}
    for sub in subreddits:
        try:
            resp = requests.get(
                f"https://old.reddit.com/r/{sub}/hot.json?limit={limit}",
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            for child in data.get("data", {}).get("children", []):
                p = child.get("data", {})
                if p.get("title") and not p.get("stickied"):
                    posts.append(
                        {
                            "title": p["title"],
                            "url": f"https://reddit.com{p.get('permalink', '')}",
                            "score": p.get("score", 0),
                            "source": f"r/{sub}",
                            "selftext": p.get("selftext", "")[:2000],
                        }
                    )
        except Exception as e:
            print(f"  [Reddit] r/{sub} failed: {e}")
    posts.sort(key=lambda x: x["score"], reverse=True)
    print(f"  [Reddit] Got {len(posts)} posts")
    return posts


def fetch_google_news(queries):
    """Fetch headlines from Google News RSS for given search queries."""
    print(f"  [Google News] Searching: {', '.join(queries)}...")
    articles = []
    for query in queries:
        try:
            url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                articles.append(
                    {
                        "title": entry.get("title", ""),
                        "url": entry.get("link", ""),
                        "source": "Google News",
                    }
                )
        except Exception as e:
            print(f"  [Google News] Query '{query}' failed: {e}")
    print(f"  [Google News] Got {len(articles)} articles")
    return articles


def fetch_techcrunch():
    """Fetch latest articles from TechCrunch RSS feed (full summary)."""
    print("  [TechCrunch] Fetching RSS feed...")
    try:
        feed = feedparser.parse("https://techcrunch.com/feed/")
        articles = []
        for entry in feed.entries[:20]:
            summary = entry.get("summary", "")
            # Clean HTML from RSS summary
            if summary:
                summary = BeautifulSoup(summary, "lxml").get_text(separator=" ")
            articles.append(
                {
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "summary": summary,
                    "source": "TechCrunch",
                }
            )
        print(f"  [TechCrunch] Got {len(articles)} articles")
        return articles
    except Exception as e:
        print(f"  [TechCrunch] Failed: {e}")
        return []


def fetch_producthunt():
    """Fetch trending products from Product Hunt RSS feed (full summary)."""
    print("  [Product Hunt] Fetching RSS feed...")
    try:
        feed = feedparser.parse("https://www.producthunt.com/feed")
        products = []
        for entry in feed.entries[:15]:
            summary = entry.get("summary", "")
            if summary:
                summary = BeautifulSoup(summary, "lxml").get_text(separator=" ")
            products.append(
                {
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "summary": summary,
                    "source": "Product Hunt",
                }
            )
        print(f"  [Product Hunt] Got {len(products)} products")
        return products
    except Exception as e:
        print(f"  [Product Hunt] Failed: {e}")
        return []


# ─── Article Extraction ──────────────────────────────────────────────────────

def extract_article(url, max_chars=3000):
    """Extract article text from a URL using trafilatura."""
    if not url:
        return None
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(
                downloaded,
                include_comments=False,
                include_tables=False,
                favor_recall=True,
            )
            if text:
                return text[:max_chars]
    except Exception as e:
        print(f"    Article extraction failed for {url[:60]}: {e}")
    return None


def enrich_trends(trends, max_article_chars=3000):
    """Enrich trends with article content extracted from their URLs."""
    urls_to_fetch = []
    for i, t in enumerate(trends):
        if t.get("url") and t["source"] != "User":
            urls_to_fetch.append((i, t["url"]))

    if not urls_to_fetch:
        return trends

    print(f"\n  Extracting article content for {len(urls_to_fetch)} URLs...")

    def _extract(idx_url):
        idx, url = idx_url
        text = extract_article(url, max_chars=max_article_chars)
        return idx, text

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_extract, iu): iu for iu in urls_to_fetch}
        success = 0
        for fut in as_completed(futures):
            try:
                idx, text = fut.result()
                if text:
                    trends[idx]["article_text"] = text
                    success += 1
            except Exception:
                pass

    print(f"  Extracted {success}/{len(urls_to_fetch)} articles successfully")
    return trends


# ─── Trend Aggregation ───────────────────────────────────────────────────────

def aggregate_trends(config, extra_themes=None):
    """Fetch from all enabled sources, deduplicate, return top trends."""
    sources = config["trends"]["sources"]
    max_trends = config["trends"]["max_trends"]
    all_items = []

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = []
        if sources.get("hackernews"):
            hn_comments = config.get("research", {}).get("hn_comments_per_story", 5)
            futures.append(pool.submit(fetch_hackernews, comments_per_story=hn_comments))
        if sources.get("reddit"):
            futures.append(
                pool.submit(fetch_reddit, config["trends"]["reddit_subs"])
            )
        if sources.get("google_news"):
            futures.append(
                pool.submit(fetch_google_news, config["trends"]["news_queries"])
            )
        if sources.get("techcrunch"):
            futures.append(pool.submit(fetch_techcrunch))
        if sources.get("producthunt"):
            futures.append(pool.submit(fetch_producthunt))

        for fut in as_completed(futures):
            try:
                all_items.extend(fut.result())
            except Exception:
                pass

    # Deduplicate by normalized title
    seen = set()
    unique = []
    for item in all_items:
        key = re.sub(r"[^a-z0-9]", "", item["title"].lower())[:60]
        if key not in seen:
            seen.add(key)
            unique.append(item)

    unique.sort(key=lambda x: x.get("score", 0), reverse=True)
    trends = unique[:max_trends]

    # Merge user-supplied themes
    if extra_themes:
        for theme in extra_themes:
            trends.insert(0, {"title": theme, "source": "User", "url": ""})

    return trends


# ─── Research Phase ──────────────────────────────────────────────────────────

def save_research(trends, ideas, config):
    """Save enriched trends + ideas + config to research JSON."""
    out_dir = ROOT / config["output"]["dir"]
    out_dir.mkdir(exist_ok=True)

    research = {
        "generated_at": datetime.now().isoformat(),
        "author": config["author"],
        "frameworks": [
            {"name": fw["name"], "instruction": fw["instruction"], "example_hook": fw["example_hook"]}
            for fw in FRAMEWORKS
        ],
        "user_ideas": ideas,
        "trends": [],
    }

    for i, t in enumerate(trends):
        entry = {
            "id": i + 1,
            "title": t.get("title", ""),
            "url": t.get("url", ""),
            "source": t.get("source", ""),
            "score": t.get("score", 0),
        }
        if t.get("summary"):
            entry["summary"] = t["summary"]
        if t.get("article_text"):
            entry["article_text"] = t["article_text"]
        if t.get("hn_comments"):
            entry["hn_comments"] = t["hn_comments"]
        if t.get("hn_id"):
            entry["hn_id"] = t["hn_id"]
        if t.get("selftext"):
            entry["selftext"] = t["selftext"]
        research["trends"].append(entry)

    filename = f"research_{date.today().isoformat()}.json"
    out_path = out_dir / filename
    with open(out_path, "w") as f:
        json.dump(research, f, indent=2, ensure_ascii=False)

    # Print summary
    with_articles = sum(1 for t in research["trends"] if t.get("article_text"))
    with_comments = sum(1 for t in research["trends"] if t.get("hn_comments"))
    total_comments = sum(len(t.get("hn_comments", [])) for t in research["trends"])

    print(f"\nResearch saved to: {out_path}")
    print(f"  Trends: {len(research['trends'])}")
    print(f"  With article text: {with_articles}")
    print(f"  With HN comments: {with_comments} ({total_comments} total comments)")
    print(f"  User ideas: {len(ideas)}")

    return out_path


# ─── Post Generation (API-based, optional) ───────────────────────────────────

FRAMEWORKS = [
    {
        "name": "Hot Take",
        "instruction": "Write a contrarian or bold opinion about a trending topic. Challenge conventional wisdom. Be specific — name the trend or tool. End with a sharp takeaway, not a question.",
        "example_hook": "Everyone's building AI wrappers. Here's why that's actually smart.",
    },
    {
        "name": "Story",
        "instruction": "Tell a personal anecdote (real or realistic) about building, failing, or learning something in tech. Start with a vivid moment, build tension, land on a concrete lesson.",
        "example_hook": "Last week I broke prod at 2 AM. Here's what I learned.",
    },
    {
        "name": "How I Built X",
        "instruction": "Walk through a mini experiment or demo: what you built, how (include a code snippet or tool stack), and the result. Keep it practical and replicable. Suggest what the reader could try.",
        "example_hook": "I built an AI agent that does X in 30 lines of Python. Here's how.",
    },
    {
        "name": "Listicle / Tips",
        "instruction": "Share 3-7 numbered insights or tips from real experience. Each point should be specific and actionable, not generic advice. Use short punchy sentences.",
        "example_hook": "5 things I learned building with Claude API this month",
    },
    {
        "name": "Data / Insight",
        "instruction": "Present a surprising observation, comparison, or data point. Show your methodology briefly. Draw a non-obvious conclusion. Use specific numbers even if estimated.",
        "example_hook": "I tested 4 LLMs on the same task. The results surprised me.",
    },
    {
        "name": "Question Post",
        "instruction": "Pose a genuine, thought-provoking question that invites real opinions (not yes/no). Provide brief context for why you're asking. Share your own tentative answer to prime the discussion.",
        "example_hook": "What's the one AI tool you'd keep if you could only pick one?",
    },
    {
        "name": "Building in Public",
        "instruction": "Share a progress update on something you're building. Include specific numbers (users, revenue, lines of code, days). Be honest about failures and wins. End with what's next.",
        "example_hook": "Week 12 of building [product]. Revenue: $0. Learnings: priceless.",
    },
    # ── EDUTAINMENT ───────────────────────────────────────────────────────────
    {
        "name": "Edutainment: 1000 Hours -> 5 Minutes",
        "instruction": (
            "Take something you spent weeks/months learning and compress it into a "
            "5-minute read. You are a top expert translating deep knowledge for a "
            "beginner. Use concrete examples, a code snippet or diagram if it helps. "
            "The reader should walk away feeling they just skipped a painful learning "
            "curve. Write it like you're explaining to a smart friend over coffee — "
            "NOT like a textbook or press release."
        ),
        "example_hook": "I spent 200 hours learning Kubernetes. Here's what actually matters in 5 minutes.",
    },
    {
        "name": "Edutainment: Free Resources Goldmine",
        "instruction": (
            "Curate a list of genuinely free resources, tools, credits, or programs "
            "that most people don't know about. Be specific — include exact names, "
            "dollar amounts, limits. No vague 'check out X'. Each item should "
            "make the reader think 'wait, this is free?' Ground it in a trending "
            "topic or a problem people are actively trying to solve. Write it like "
            "you're sharing a cheat sheet with a friend, not writing an ad."
        ),
        "example_hook": "You can build an AI startup for $0. Here are 8 free resources nobody talks about.",
    },
    {
        "name": "Edutainment: Myth Buster",
        "instruction": (
            "Take a widely believed 'truth' in tech/AI and show why it's wrong or "
            "misleading. Use real data, your own experience, or a concrete example "
            "to debunk it. Don't be preachy — be the friend who says 'actually, "
            "let me show you something.' The goal is to educate through surprise. "
            "Ground it in something trending right now."
        ),
        "example_hook": "\"You need a GPU to run LLMs locally.\" I ran one on a $300 laptop. Here's what happened.",
    },
    {
        "name": "Edutainment: How It Actually Works",
        "instruction": (
            "Pick a technology, tool, or concept that people use every day but don't "
            "truly understand. Explain the internals in simple language with a "
            "concrete walkthrough. Include a code snippet, diagram description, or "
            "step-by-step breakdown. Make the reader feel smarter, not dumber. "
            "Write like a diary entry about something you just figured out."
        ),
        "example_hook": "Everyone uses Docker. Almost nobody understands what happens when you type 'docker run'.",
    },
    # ── STORYTELLING ──────────────────────────────────────────────────────────
    {
        "name": "Story: Build in Public (Episode)",
        "instruction": (
            "Write an episode-style update — like a TV show people follow daily. "
            "Structure: where you left off → what happened today → cliffhanger or "
            "teaser for next episode. Include specific numbers (lines of code, "
            "users, revenue, bugs). Be brutally honest about failures. The reader "
            "should feel invested and want to see the next episode. Think 'diary "
            "entry' not 'press release'."
        ),
        "example_hook": "Day 14 of building my AI trading bot. Yesterday it made $12. Today it lost $47. Here's why.",
    },
    {
        "name": "Story: The Failure That Taught Me",
        "instruction": (
            "Tell a real (or realistic) story about something that went wrong — a "
            "bug, a bad decision, a project that failed. Start in the middle of the "
            "action (the 2 AM alert, the angry customer, the demo that crashed). "
            "Build tension. Land on a specific, non-obvious lesson. Write it like "
            "you're telling a friend at a bar. NO generic morals like 'failure is "
            "the best teacher'. The lesson should be tactical and useful."
        ),
        "example_hook": "I shipped a feature on Friday. By Monday, 2,000 users had bad data. Here's the one line I missed.",
    },
    {
        "name": "Story: Behind the Scenes",
        "instruction": (
            "Pull back the curtain on something people normally don't see — how a "
            "system really works, what a day actually looks like, how a decision "
            "was really made. Use specific details: timestamps, tool names, exact "
            "numbers. The reader should feel like they're getting insider access. "
            "Write it as a narrative with a beginning, middle, and end — not a "
            "listicle wearing a story's clothes."
        ),
        "example_hook": "Here's what actually happens when your algo trading bot places an order at 9:15 AM.",
    },
]

SYSTEM_PROMPT = """\
You are a LinkedIn ghostwriter for {name}, a {role} in the {domain} space.

CONTENT PHILOSOPHY:
The best-performing content falls into two categories — Edutainment and Storytelling. \
Lean heavily into these. Avoid controversy/hate-bait, vague wisdom one-liners, and \
generic "spark a conversation" posts.

EDUTAINMENT = You are a top expert who spent 1000 hours learning something. \
Turn that into a 5-minute read so the reader skips the painful part. \
Think: free resource lists, myth-busting, "how it actually works" deep dives.

STORYTELLING = You are a TV show. Every post is an episode people want to follow. \
Think: build-in-public updates, failure stories, behind-the-scenes narratives. \
Make people anticipate the next post.

VOICE & TONE:
- Write like a diary entry or like you're telling a story to a friend
- NEVER write like a press release, ad campaign, or corporate announcement
- First-person, conversational, confident but not arrogant
- Mix of: {tone_mix}
- No corporate jargon, no cringe motivational quotes, no "I'm humbled"

STRUCTURE (every post):
1. Hook line — the first 1-2 lines must stop the scroll. Be specific, surprising, or provocative.
2. Body — deliver real value: a story, insight, data, or walkthrough. Use short paragraphs (1-2 lines each). Blank lines between paragraphs.
3. CTA / Takeaway — end with a concrete lesson or a teaser for the next episode. Never "Agree?" or "Thoughts?"
4. Hashtags — exactly 3-5 relevant hashtags at the very end.

FORMATTING RULES:
- Short paragraphs (1-2 lines max), separated by blank lines
- Occasional **bold** for emphasis (sparingly)
- No emoji spam — at most 1-2 per post, only if they add meaning
- Posts should be 150-300 words

ANTI-PATTERNS (never do these):
- "I'm humbled/excited to announce..."
- Emoji walls or bullet-point-with-emoji lists
- Generic platitudes ("hard work pays off", "the future is now")
- Vague wisdom one-liners with no substance behind them
- Controversy or hate-bait just to get engagement
- Ending with "Agree?" or "What do you think?" with no context
- Fake humility or humble-bragging
- Mentioning "LinkedIn" or "this platform"
- Low-effort generic takes — every post should feel like it took 2+ hours of thought

QUALITY BAR:
- Every post must teach something specific OR advance a narrative people follow
- If you can swap out {name}'s name and the post still works for anyone, it's too generic
- Include specific details, numbers, tool names, code references when relevant
- Admit failures and uncertainties — it builds trust
- Reference actual trends and news happening right now
- {name}'s interests: {interests}

You will be given trending topics/news to ground each post in reality. Use them as inspiration — don't just summarize them.
"""


def build_user_prompt(trends, frameworks, experiment=None, total_posts=None):
    """Build the user message with trends and framework instructions."""
    num_posts = total_posts or len(frameworks)

    trend_text = "## Current Trending Topics & News\n\n"
    for i, t in enumerate(trends, 1):
        line = f"{i}. [{t['source']}] {t['title']}"
        if t.get("url"):
            line += f" ({t['url']})"
        if t.get("summary"):
            line += f"\n   Summary: {t['summary']}"
        trend_text += line + "\n"

    framework_text = f"\n## Post Frameworks\n\nGenerate exactly {num_posts} LinkedIn posts. Use the frameworks below — one post per framework first, then reuse the best-fitting frameworks (especially Edutainment and Storytelling ones) for additional posts on different trending topics.\n\n"
    for i, fw in enumerate(frameworks, 1):
        framework_text += f"### Framework {i}: {fw['name']}\n"
        framework_text += f"Instruction: {fw['instruction']}\n"
        framework_text += f"Example hook style: \"{fw['example_hook']}\"\n\n"

    experiment_text = ""
    if experiment:
        experiment_text = f"\n## Experiment Idea\nFor the 'How I Built X' post (Post 3), base it around this experiment concept: \"{experiment}\"\nInclude a suggested code snippet or tool stack.\n\n"

    output_format = f"""
## Output Format

For each post, output:

---
### Post N: [Framework Name]
IMAGE_QUERY: [A specific search query to find a relevant image for this post. \
Think: tweet screenshots, product screenshots, charts, news headlines, memes. \
Be specific — e.g. "elon musk naval tweet coding" not "AI technology". \
Prefer queries that would find real screenshots, tweets, or visuals over stock photos.]

[The full LinkedIn post text, ready to copy-paste]

---

Do NOT include any commentary, just the {num_posts} posts.
"""

    return trend_text + framework_text + experiment_text + output_format


def generate_posts(config, trends, experiment=None):
    """Call Claude API to generate posts."""
    import anthropic

    author = config["author"]
    gen = config["generation"]
    total = gen["posts_per_batch"]

    system = SYSTEM_PROMPT.format(
        name=author["name"],
        role=author["role"],
        domain=author["domain"],
        tone_mix=", ".join(author["tone_mix"]),
        interests=", ".join(author["interests"]),
    )

    user_msg = build_user_prompt(trends, FRAMEWORKS, experiment, total_posts=total)

    client = anthropic.Anthropic()
    print(f"\nGenerating {total} posts with {gen['model']}...")

    response = client.messages.create(
        model=gen["model"],
        max_tokens=gen["max_tokens"] * total,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )

    return response.content[0].text


def save_output(content, config):
    """Save generated posts to output directory."""
    out_dir = ROOT / config["output"]["dir"]
    out_dir.mkdir(exist_ok=True)
    filename = f"batch_{date.today().isoformat()}.md"
    out_path = out_dir / filename

    header = f"# LinkedIn Posts — Batch of {date.today().isoformat()}\n"
    header += f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"

    with open(out_path, "w") as f:
        f.write(header + content)

    return out_path


# ─── Image Generation ─────────────────────────────────────────────────────────

GRADIENTS = [
    [(15, 23, 42), (88, 28, 135)],       # dark blue -> purple
    [(17, 24, 39), (5, 150, 105)],        # dark navy -> teal
    [(30, 27, 75), (219, 39, 119)],       # indigo -> pink
    [(20, 20, 20), (234, 88, 12)],        # charcoal -> orange
    [(15, 23, 42), (37, 99, 235)],        # dark -> bright blue
    [(39, 21, 52), (220, 38, 38)],        # dark purple -> red
    [(10, 30, 30), (6, 182, 212)],        # dark teal -> cyan
]

FONT_PATH = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
IMG_W, IMG_H = 1200, 628


def _lerp_color(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def _draw_gradient(draw, w, h, c1, c2):
    for y in range(h):
        color = _lerp_color(c1, c2, y / h)
        draw.line([(0, y), (w, y)], fill=color)


def _wrap_text(text, font, max_width, draw):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > max_width and current:
            lines.append(current)
            current = word
        else:
            current = test
    if current:
        lines.append(current)
    return lines


def generate_post_image(hook_text, post_num, out_dir):
    img = Image.new("RGB", (IMG_W, IMG_H))
    draw = ImageDraw.Draw(img)

    c1, c2 = GRADIENTS[post_num % len(GRADIENTS)]
    _draw_gradient(draw, IMG_W, IMG_H, c1, c2)

    circle_x = IMG_W * 0.75
    circle_y = IMG_H * 0.3
    circle_r = 180
    for r in range(int(circle_r), 0, -1):
        shade = _lerp_color(c2, (255, 255, 255), 0.3)
        draw.ellipse(
            [circle_x - r, circle_y - r, circle_x + r, circle_y + r],
            outline=(*shade,),
        )

    padding = 80
    max_text_w = IMG_W - padding * 2
    font_size = 52
    while font_size > 28:
        font = ImageFont.truetype(FONT_PATH, font_size)
        lines = _wrap_text(hook_text, font, max_text_w, draw)
        line_h = font_size * 1.4
        total_h = len(lines) * line_h
        if total_h < IMG_H - padding * 2 and len(lines) <= 6:
            break
        font_size -= 2

    y_start = (IMG_H - total_h) / 2
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        x = (IMG_W - text_w) / 2
        y = y_start + i * line_h
        draw.text((x + 2, y + 2), line, fill=(0, 0, 0, 128), font=font)
        draw.text((x, y), line, fill=(255, 255, 255), font=font)

    line_y = IMG_H - 40
    line_w = 60
    draw.line(
        [(IMG_W / 2 - line_w, line_y), (IMG_W / 2 + line_w, line_y)],
        fill=(255, 255, 255, 180),
        width=3,
    )

    filename = f"post_{post_num + 1}.png"
    path = out_dir / filename
    img.save(path, "PNG")
    return path


def extract_hooks(content):
    hooks = []
    posts = re.split(r"###\s*Post\s*\d+\s*:", content)
    for block in posts[1:]:
        lines = block.strip().split("\n")
        for line in lines[1:]:
            cleaned = line.strip().strip("*").strip("#").strip("-").strip()
            if (
                cleaned
                and len(cleaned) > 10
                and not cleaned.startswith("---")
                and not cleaned.startswith("IMAGE_QUERY:")
            ):
                hooks.append(cleaned)
                break
    return hooks


def extract_image_queries(content):
    queries = []
    posts = re.split(r"###\s*Post\s*\d+\s*:", content)
    for block in posts[1:]:
        query = None
        for line in block.strip().split("\n"):
            if line.strip().startswith("IMAGE_QUERY:"):
                query = line.strip().replace("IMAGE_QUERY:", "").strip()
                break
        queries.append(query)
    return queries


def search_and_download_image(query, post_num, out_dir, timeout=10):
    try:
        from duckduckgo_search import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.images(query, max_results=5))

        if not results:
            return None

        for result in results[:3]:
            img_url = result.get("image")
            if not img_url:
                continue
            try:
                resp = requests.get(img_url, timeout=timeout, stream=True,
                                    headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "")
                if "image" not in content_type:
                    continue

                ext = "jpg"
                if "png" in content_type:
                    ext = "png"
                elif "webp" in content_type:
                    ext = "webp"

                filename = f"post_{post_num + 1}_img.{ext}"
                path = out_dir / filename
                with open(path, "wb") as f:
                    for chunk in resp.iter_content(8192):
                        f.write(chunk)

                img = Image.open(path)
                img.verify()
                return path

            except Exception:
                continue

    except Exception as e:
        print(f"  Image search failed for post {post_num + 1}: {e}")

    return None


def generate_images_from_content(content, config):
    """Find relevant images for each post, falling back to gradient quote cards."""
    out_dir = ROOT / config["output"]["dir"]
    out_dir.mkdir(exist_ok=True)

    hooks = extract_hooks(content)
    image_queries = extract_image_queries(content)

    if not hooks:
        print("Warning: Could not extract hooks from posts, skipping images")
        return []

    paths = []
    for i, hook in enumerate(hooks):
        query = image_queries[i] if i < len(image_queries) else None
        path = None

        if query:
            print(f"  Post {i + 1}: Searching \"{query}\"...")
            time.sleep(3)  # Rate limit for DuckDuckGo
            path = search_and_download_image(query, i, out_dir)
            if path:
                print(f"  Post {i + 1}: Found image -> {path.name}")

        if path is None:
            path = generate_post_image(hook, i, out_dir)
            print(f"  Post {i + 1}: Generated quote card -> {path.name}")

        paths.append(path)

    return paths


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LinkedIn Post Generator — Two-Phase Pipeline"
    )
    parser.add_argument(
        "--theme", action="append", default=[],
        help="Additional theme(s) to include (can repeat)",
    )
    parser.add_argument(
        "--no-trends", action="store_true",
        help="Skip trend fetching, use only --theme args",
    )
    parser.add_argument(
        "--experiment", type=str, default=None,
        help="Experiment idea for the 'How I Built X' post",
    )
    parser.add_argument(
        "--no-images", action="store_true",
        help="Skip image generation",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Path to config file (default: config.yaml)",
    )
    # New flags for two-phase pipeline
    parser.add_argument(
        "--research-only", action="store_true",
        help="Phase 1: Fetch trends, enrich with article content, save research JSON. No post generation.",
    )
    parser.add_argument(
        "--images-only", action="store_true",
        help="Phase 3: Generate images from an existing batch markdown file.",
    )
    parser.add_argument(
        "--input", type=str, default=None,
        help="Input file for --images-only (path to batch markdown)",
    )
    args = parser.parse_args()

    # Load config
    config_path = Path(args.config) if args.config else CONFIG_PATH
    with open(config_path) as f:
        config = yaml.safe_load(f)

    # ── Phase 3: Images only ──────────────────────────────────────────────
    if args.images_only:
        if not args.input:
            print("Error: --images-only requires --input <path_to_batch.md>")
            sys.exit(1)
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"Error: File not found: {input_path}")
            sys.exit(1)
        print(f"Generating images from: {input_path}")
        content = input_path.read_text()
        generate_images_from_content(content, config)
        return

    # ── Load ideas ────────────────────────────────────────────────────────
    ideas = load_ideas()
    all_themes = args.theme + ideas
    if ideas:
        print(f"Loaded {len(ideas)} idea(s) from ideas.txt")

    if args.no_trends and not all_themes:
        print("Error: --no-trends requires at least one --theme or idea in ideas.txt")
        sys.exit(1)

    # ── Fetch trends ──────────────────────────────────────────────────────
    if args.no_trends:
        print("Skipping trend fetching (--no-trends)")
        trends = [{"title": t, "source": "User", "url": ""} for t in all_themes]
    else:
        print("Fetching real-time trends...\n")
        trends = aggregate_trends(config, extra_themes=all_themes)

    if not trends:
        print("Warning: No trends found. Using fallback themes.")
        trends = [
            {"title": "AI agents and autonomous workflows", "source": "Fallback", "url": ""},
            {"title": "Open-source LLMs catching up to closed models", "source": "Fallback", "url": ""},
            {"title": "Building in public as a growth strategy", "source": "Fallback", "url": ""},
        ]

    print(f"\nTop trends ({len(trends)}):")
    for i, t in enumerate(trends[:10], 1):
        print(f"  {i}. [{t['source']}] {t['title']}")

    # ── Phase 1: Research only ────────────────────────────────────────────
    if args.research_only:
        # Enrich trends with article content
        max_chars = config.get("research", {}).get("max_article_chars", 3000)
        trends = enrich_trends(trends, max_article_chars=max_chars)
        save_research(trends, ideas, config)
        return

    # ── Full pipeline (legacy: requires ANTHROPIC_API_KEY) ────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("\nNo ANTHROPIC_API_KEY set.")
        print("Use --research-only to generate research JSON, then use Claude Code to write posts.")
        sys.exit(1)

    content = generate_posts(config, trends, experiment=args.experiment)

    out_path = save_output(content, config)
    print(f"\nSaved to: {out_path}")

    if not args.no_images:
        print("\nGenerating post images...")
        img_paths = generate_images_from_content(content, config)
        if img_paths:
            print(f"Generated {len(img_paths)} images in {ROOT / config['output']['dir']}/")
    else:
        print("\nSkipping image generation (--no-images)")

    print("\n" + "=" * 60)
    print(content)
    print("=" * 60)


if __name__ == "__main__":
    main()
