#!/usr/bin/env python3
"""LinkedIn Post Generator — fetch real-time trends, generate 7 posts via Claude."""

import argparse
import json
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic
import feedparser
import requests
import yaml

# ─── Config ───────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ─── Trend Fetching ──────────────────────────────────────────────────────────

def fetch_hackernews(limit=30):
    """Fetch top stories from Hacker News public API."""
    print("  [HN] Fetching top stories...")
    try:
        resp = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10
        )
        resp.raise_for_status()
        story_ids = resp.json()[:limit]

        stories = []
        # Fetch stories in parallel for speed
        def _get(sid):
            r = requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=10
            )
            r.raise_for_status()
            return r.json()

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_get, sid): sid for sid in story_ids}
            for fut in as_completed(futures):
                try:
                    item = fut.result()
                    if item and item.get("title"):
                        stories.append(
                            {
                                "title": item["title"],
                                "url": item.get("url", ""),
                                "score": item.get("score", 0),
                                "source": "Hacker News",
                            }
                        )
                except Exception:
                    pass

        stories.sort(key=lambda x: x["score"], reverse=True)
        print(f"  [HN] Got {len(stories)} stories")
        return stories
    except Exception as e:
        print(f"  [HN] Failed: {e}")
        return []


def fetch_reddit(subreddits, limit=15):
    """Fetch hot posts from Reddit subreddits via public JSON API."""
    print(f"  [Reddit] Fetching from r/{', r/'.join(subreddits)}...")
    posts = []
    headers = {"User-Agent": "linkedin-gen/1.0"}
    for sub in subreddits:
        try:
            resp = requests.get(
                f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}",
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
    """Fetch latest articles from TechCrunch RSS feed."""
    print("  [TechCrunch] Fetching RSS feed...")
    try:
        feed = feedparser.parse("https://techcrunch.com/feed/")
        articles = []
        for entry in feed.entries[:20]:
            articles.append(
                {
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "summary": entry.get("summary", "")[:200],
                    "source": "TechCrunch",
                }
            )
        print(f"  [TechCrunch] Got {len(articles)} articles")
        return articles
    except Exception as e:
        print(f"  [TechCrunch] Failed: {e}")
        return []


def fetch_producthunt():
    """Fetch trending products from Product Hunt RSS feed."""
    print("  [Product Hunt] Fetching RSS feed...")
    try:
        feed = feedparser.parse("https://www.producthunt.com/feed")
        products = []
        for entry in feed.entries[:15]:
            products.append(
                {
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "summary": entry.get("summary", "")[:200],
                    "source": "Product Hunt",
                }
            )
        print(f"  [Product Hunt] Got {len(products)} products")
        return products
    except Exception as e:
        print(f"  [Product Hunt] Failed: {e}")
        return []


def aggregate_trends(config, extra_themes=None):
    """Fetch from all enabled sources, deduplicate, return top trends."""
    sources = config["trends"]["sources"]
    max_trends = config["trends"]["max_trends"]
    all_items = []

    # Fetch in parallel
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = []
        if sources.get("hackernews"):
            futures.append(pool.submit(fetch_hackernews))
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

    # Sort by score (if available), take top N
    unique.sort(key=lambda x: x.get("score", 0), reverse=True)
    trends = unique[:max_trends]

    # Merge user-supplied themes
    if extra_themes:
        for theme in extra_themes:
            trends.insert(0, {"title": theme, "source": "User", "url": ""})

    return trends


# ─── Post Generation ─────────────────────────────────────────────────────────

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
]

SYSTEM_PROMPT = """\
You are a LinkedIn ghostwriter for {name}, a {role} in the {domain} space.

VOICE & TONE:
- First-person, conversational, confident but not arrogant
- Mix of: {tone_mix}
- No corporate jargon, no cringe motivational quotes, no "I'm humbled"
- Write like a smart friend explaining something interesting over coffee

STRUCTURE (every post):
1. Hook line — the first 1-2 lines must stop the scroll. Be specific, surprising, or provocative.
2. Body — deliver real value: a story, insight, data, or walkthrough. Use short paragraphs (1-2 lines each). Blank lines between paragraphs.
3. CTA / Takeaway — end with a clear takeaway or a genuine question. Never "Agree?" or "Thoughts?"
4. Hashtags — exactly 3-5 relevant hashtags at the very end.

FORMATTING RULES:
- Short paragraphs (1-2 lines max), separated by blank lines
- Occasional **bold** for emphasis (sparingly)
- No emoji spam — at most 1-2 per post, only if they add meaning
- Posts should be 150-250 words (LinkedIn sweet spot)

ANTI-PATTERNS (never do these):
- "I'm humbled/excited to announce..."
- Emoji walls or bullet-point-with-emoji lists
- Generic platitudes ("hard work pays off", "the future is now")
- Ending with "Agree?" or "What do you think?" with no context
- Fake humility or humble-bragging
- Mentioning "LinkedIn" or "this platform"

AUTHENTICITY:
- Include specific details, numbers, tool names, code references when relevant
- Admit failures and uncertainties — it builds trust
- Reference actual trends and news happening right now
- {name}'s interests: {interests}

You will be given trending topics/news to ground each post in reality. Use them as inspiration — don't just summarize them.
"""


def build_user_prompt(trends, frameworks, experiment=None):
    """Build the user message with trends and framework instructions."""
    trend_text = "## Current Trending Topics & News\n\n"
    for i, t in enumerate(trends, 1):
        line = f"{i}. [{t['source']}] {t['title']}"
        if t.get("url"):
            line += f" ({t['url']})"
        if t.get("summary"):
            line += f"\n   Summary: {t['summary']}"
        trend_text += line + "\n"

    framework_text = "\n## Post Frameworks\n\nGenerate exactly 7 LinkedIn posts, one for each framework below. Ground each post in one or more of the trending topics above.\n\n"
    for i, fw in enumerate(frameworks, 1):
        framework_text += f"### Post {i}: {fw['name']}\n"
        framework_text += f"Instruction: {fw['instruction']}\n"
        framework_text += f"Example hook style: \"{fw['example_hook']}\"\n\n"

    experiment_text = ""
    if experiment:
        experiment_text = f"\n## Experiment Idea\nFor the 'How I Built X' post (Post 3), base it around this experiment concept: \"{experiment}\"\nInclude a suggested code snippet or tool stack.\n\n"

    output_format = """
## Output Format

For each post, output:

---
### Post N: [Framework Name]

[The full LinkedIn post text, ready to copy-paste]

---

Do NOT include any commentary, just the 7 posts.
"""

    return trend_text + framework_text + experiment_text + output_format


def generate_posts(config, trends, experiment=None):
    """Call Claude API to generate posts."""
    author = config["author"]
    gen = config["generation"]

    system = SYSTEM_PROMPT.format(
        name=author["name"],
        role=author["role"],
        domain=author["domain"],
        tone_mix=", ".join(author["tone_mix"]),
        interests=", ".join(author["interests"]),
    )

    user_msg = build_user_prompt(trends, FRAMEWORKS, experiment)

    client = anthropic.Anthropic()
    print(f"\nGenerating {gen['posts_per_batch']} posts with {gen['model']}...")

    response = client.messages.create(
        model=gen["model"],
        max_tokens=gen["max_tokens"] * gen["posts_per_batch"],
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )

    return response.content[0].text


def save_output(content, config):
    """Save generated posts to output directory."""
    out_dir = ROOT / config["output"]["dir"]
    out_dir.mkdir(exist_ok=True)
    filename = f"week_{date.today().isoformat()}.md"
    out_path = out_dir / filename

    header = f"# LinkedIn Posts — Week of {date.today().isoformat()}\n"
    header += f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"

    with open(out_path, "w") as f:
        f.write(header + content)

    return out_path


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate LinkedIn posts grounded in real-time trends"
    )
    parser.add_argument(
        "--theme",
        action="append",
        default=[],
        help="Additional theme(s) to include (can repeat)",
    )
    parser.add_argument(
        "--no-trends",
        action="store_true",
        help="Skip trend fetching, use only --theme args",
    )
    parser.add_argument(
        "--experiment",
        type=str,
        default=None,
        help="Experiment idea for the 'How I Built X' post",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config file (default: config.yaml)",
    )
    args = parser.parse_args()

    # Load config
    config_path = Path(args.config) if args.config else CONFIG_PATH
    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Validate
    if args.no_trends and not args.theme:
        print("Error: --no-trends requires at least one --theme")
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable not set")
        sys.exit(1)

    # Step 1: Fetch trends
    if args.no_trends:
        print("Skipping trend fetching (--no-trends)")
        trends = [{"title": t, "source": "User", "url": ""} for t in args.theme]
    else:
        print("Fetching real-time trends...\n")
        trends = aggregate_trends(config, extra_themes=args.theme)

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

    # Step 2: Generate posts
    content = generate_posts(config, trends, experiment=args.experiment)

    # Save
    out_path = save_output(content, config)
    print(f"\nSaved to: {out_path}")

    # Also print to terminal
    print("\n" + "=" * 60)
    print(content)
    print("=" * 60)


if __name__ == "__main__":
    main()
