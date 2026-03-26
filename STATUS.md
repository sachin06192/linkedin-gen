# LinkedIn Post Generator — Project Status

**Last updated:** 2026-03-26
**Last session:** Deep research pipeline + batch generation

---

## What's Built

### Two-Phase Pipeline (`generate.py`)
- **Phase 1:** `python generate.py --research-only` — fetches trends from HN/Google News/TechCrunch/Product Hunt, enriches with full article text (trafilatura), HN comments (top 5 per story), full RSS summaries. Saves `output/research_YYYY-MM-DD.json`.
- **Phase 2:** Claude Code session reads research JSON, does per-post deep research via WebFetch, writes posts with `<details>` research notes.
- **Phase 3:** `python generate.py --images-only --input output/batch.md` — DuckDuckGo image search per post, gradient quote card fallback.

### Frameworks (14 total)
- 7 original: Hot Take, Story, How I Built X, Listicle/Tips, Data/Insight, Question Post, Building in Public
- 4 edutainment: 1000hrs->5min, Free Resources Goldmine, Myth Buster, How It Actually Works
- 3 storytelling: Build in Public Episode, Failure That Taught Me, Behind the Scenes

### System Prompt
- Edutainment + Storytelling philosophy baked in
- Anti-patterns: no press release tone, no generic takes, no controversy/hate-bait
- Quality bar: specific numbers, real URLs, personal angle required

---

## Current Problem: Posts Still Feel AI-Generated

Despite real research data (article text, HN comments, WebFetch deep dives), posts still lack "soul." They're too clean, too structured, too predictable. They read like "AI writing about a topic" not "Sachin sharing something he experienced."

### Root Cause
- No analysis of what actually works on LinkedIn (viral post patterns)
- Frameworks were designed by AI, not derived from real viral posts
- Voice is generic "LinkedIn thought leader" not Sachin's actual voice
- Image selection is weak (DuckDuckGo rate limits, generic results)

---

## Next Steps (In Order)

### 1. Build Swipe File (Sachin — via Cowork on desktop)
- Browse LinkedIn, save 20-30 high-engagement posts that feel authentic
- Capture: text, image type, engagement numbers, why it works
- Also capture viral tech tweets for screenshot images
- Template ready in `swipe_file.md` — fill in and push to repo

### 2. Analyze Swipe File Patterns (Claude Code)
- Extract hook styles, formatting patterns, image types, tone markers
- Identify what makes posts feel human vs. AI
- Map engagement levels to specific patterns

### 3. Rebuild Frameworks from Real Data
- Replace current 14 frameworks with patterns derived from the swipe file
- Calibrate system prompt to match real viral post voice
- Add specific formatting rules (short punchy lines, one-word paragraphs, etc.)

### 4. Voice Calibration
- Feed in 10-20 posts Sachin wrote/heavily edited that felt like "him"
- Extract his actual voice patterns vs AI defaults
- Bake into system prompt as voice examples

### 5. Implement Remaining Pipeline Improvements
- Per-post WebFetch deep research (currently done manually in session)
- Research notes with `<details>` blocks (implemented in latest batch)
- Quality gates: every post must have 1+ real URL, 1+ sourced number, personal angle
- Better image sourcing (fix DuckDuckGo rate limits, consider alternative providers)
- Topic selection logic (match to author interests, filter low-substance trends)

---

## File Structure

```
linkedin-gen/
├── generate.py          # Main pipeline (research + generation + images)
├── config.yaml          # Author profile, trend sources, generation settings
├── ideas.txt            # User ideas mixed into trend pool
├── swipe_file.md        # Template for viral post collection (TODO: fill in)
├── requirements.txt     # Python deps
├── STATUS.md            # This file
├── output/
│   ├── research_2026-03-26.json   # Enriched trends (articles + HN comments)
│   ├── batch_2026-03-26.md        # Latest posts (12, deep-researched)
│   ├── batch_2026-03-14.md        # Earlier batch (20, generic)
│   └── post_*.png / post_*_img.*  # Images per post
└── venv/                # Python virtual environment
```

---

## Config Quick Reference

```bash
# Phase 1: Fetch trends + research
cd /root/linkedin-gen && venv/bin/python generate.py --research-only

# Phase 3: Generate images from existing batch
venv/bin/python generate.py --images-only --input output/batch_2026-03-26.md

# Full pipeline (requires ANTHROPIC_API_KEY)
ANTHROPIC_API_KEY=xxx venv/bin/python generate.py
```

---

## Key Decisions Made

- **No Anthropic API key** — generation done in Claude Code session, not via API
- **trafilatura** for article extraction (best Python library for this)
- **DuckDuckGo** for image search (free, no API key, but rate limits aggressively)
- **Research notes** in `<details>` blocks below each post for user editing
- **Edutainment + Storytelling** focus based on viral content analysis writeup
