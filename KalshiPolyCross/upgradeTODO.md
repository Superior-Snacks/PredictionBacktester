# Market Discovery Pipeline — Upgrade Plan

Defer until the cross-venue arb strategy is confirmed profitable with the
current pipeline. This document captures the upgrade path so the design
work isn't lost between now and then.

## Why we're doing this eventually

The current pipeline has a **silent recall problem**: pairs that should exist
but never appear in any candidate set are missed completely, with no visibility
into what was missed. The Brazilian presidential election is a known example;
international elections, niche sports series, and abbreviated market titles
are all failure classes.

Root causes:
- Embeddings are bad at exact entity matching, numerical precision, dates,
  and structural differences
- A single ranking algorithm has no way to surface markets that score low on
  its specific signal
- No "what's been missed?" visibility — the tool only shows what it found

Solution: a multi-signal discovery pipeline with coverage reporting, where the
LLM eventually fits as a final judge of candidates rather than a retriever.

---

## STAGE 1 — Unified local index (foundation)

Everything else depends on this. The current code fetches Kalshi and
Polymarket data into separate flows; the upgrade puts them into one
searchable structure.

### [ ] Define a unified `MarketRecord` schema
Fields needed regardless of source venue:
- `venue` ("kalshi" | "poly")
- `id` (ticker for Kalshi, yes_token prefix for Poly)
- `title` (the question)
- `event_title` (parent event name; empty for standalone binaries)
- `event_id` (event_ticker on Kalshi, event_slug on Poly)
- `group_item_title` (named outcome for neg-risk / sub-markets)
- `sibling_outcomes` (list of other outcomes in the same event)
- `rules_text` (rules_primary on Kalshi, description on Poly)
- `category` (Politics, Sports, Crypto, etc.)
- `close_date` (datetime)
- `is_neg_risk` (boolean — multi-outcome mutually-exclusive event)
- `raw_record` (the full original record for downstream use)

### [ ] Build `build_unified_index()` from existing fetch outputs
- Run after `fetch_kalshi_markets()` and `fetch_poly_markets()`
- Normalize both venues' data into `MarketRecord` instances
- Return a list of records, both venues mixed together
- Memory budget: a few hundred MB max even for full corpora

### [ ] Persist the index to disk
- Cache as Parquet or compressed JSON alongside the embedding cache
- Include a timestamp so you can detect staleness
- Refresh policy: full rebuild daily, or when explicitly invalidated

---

## STAGE 2 — Multi-signal search

The index supports multiple complementary search modes. Each is independently
useful and they combine to give comprehensive recall.

### [ ] Keyword AND search (precise queries)
```
results = [m for m in index 
           if all(kw.lower() in (m.title + " " + m.rules_text).lower() 
                  for kw in query_keywords)]
```
- Hit-count tiebreaker for ranking
- Cheap, deterministic, transparent

### [ ] BM25 search (natural-language queries)
- Use `rank-bm25` library
- Index over `title + group_item_title + event_title + rules_text[:500]`
- Weight title fields more heavily by repetition trick
- Better than keyword-AND when query has many words or contains common terms

### [ ] Phrase search (entity disambiguation)
- Substring match on lowercased text
- Useful for proper nouns where word-order matters
- "Will Smith" the actor vs "smith" "will" in unrelated markets

### [ ] Entity extraction + match
- spaCy `en_core_web_lg` for NER on title + rules
- Extract PERSON, ORG, GPE (geo), MONEY, DATE entities
- Normalize (lowercase, strip punctuation, "S. Scheffler" → "scheffler")
- Two markets must share named entities to be candidate matches
- Catches the Brazilian/Bolsonaro/Lula misses where embedding fails on rare proper nouns

### [ ] Date range filter
- Filter index by close_date ± tolerance
- Default tolerance: 7 days for short-dated, 30 days for long-dated
- Combine with other signals; rarely useful alone

### [ ] Category filter
- Cross-reference Kalshi `category` with Polymarket `tags`
- Build a manual mapping table for the ~10 major categories
- Use as pre-filter, not as sole signal

### [ ] Regex search (power-user fallback)
- For when you know structure
- "Find tickers ending in -BRAZIL" or "find markets mentioning \$100k"

---

## STAGE 3 — Twin search workflow

The core interactive feature: given a market on one venue, search the other
venue using multiple signals simultaneously and return the union.

### [ ] Implement `twin_search(source_market, target_venue)`
For a Kalshi market searching against Polymarket (and vice versa):

1. Extract keywords from the source market's title + entity-extracted names
2. Run all of these in parallel:
   - BM25 with extracted keywords
   - Entity match on extracted PERSON/ORG/GPE
   - Date range: close_date ± 30 days
   - Category mapping
3. Compute a combined score per candidate:
   `score = w1·bm25 + w2·entity_overlap + w3·date_proximity + w4·category_match`
4. Return top-20 ranked candidates

### [ ] Auto-suggest keywords from Kalshi ticker structure
Kalshi tickers encode meaning (KXPGAWINNER-PGC26-SCHEFFLER → "PGA", "Scheffler", "2026").
Use the ticker as input to the twin search to pre-populate the query.
- Parse series_ticker out of the full ticker
- Look up series title from cached series list
- Extract obvious entity codes from the suffix

### [ ] Display twin search results in two-column format
```
SOURCE (Kalshi):                    CANDIDATES (Polymarket):
KXBRAZILPRES-26-LULA                  1. "Will Lula win Brazil 2026?"   [BM25+entity]
"Will Lula da Silva win..."           2. "Will PT party win..."         [entity+date]
event: 2026 Brazil Presidential        3. "Who is Brazil's next pres?"  [date+category]
                                       ...
```

---

## STAGE 4 — Coverage reporting

This is what makes the "what's been missed?" problem visible.

### [ ] Per-event coverage report
For every Kalshi event and Polymarket event, compute:
- Total sub-markets in the event
- Sub-markets paired (in `cross_pairs.json`)
- Sub-markets rejected (in `rejected_pairs.json`)
- Sub-markets with at least one embedding-candidate suggestion
- Sub-markets with zero attempted candidates (silent miss class)

Output as CSV or interactive console table sorted by "silent miss count desc".

### [ ] Per-category coverage report
- How many events in each category are fully paired?
- Which categories have the highest unpaired rate?
- Tells you where to focus discovery work

### [ ] "Untouched events" report
List of Kalshi events and Polymarket events with zero pairing attempts.
The Brazil case would have appeared here under current data.

### [ ] Periodic coverage tracking
- Save daily snapshots of coverage stats
- Track trend: are you closing the gap or falling behind?

---

## STAGE 5 — Workflow integration

Tie the new search and reporting into the existing manual review flow.

### [ ] Add `/find <query>` keypress in manual review
- Operates on the unified index
- Two-column results display
- Selecting a result creates a manual-override candidate with `score=1.0`
- Manual candidate flows into the existing verdict prompt

### [ ] Add `/twin` keypress for the current pair
- Runs twin search using the current Kalshi market as source
- Replaces the embedding-candidate list with the twin search results

### [ ] Add `/coverage` command
- Generates the coverage report on demand
- Drops you into an "explore uncovered events" mode where you can pick
  an unpaired event and run twin search on it

### [ ] Add `/event <event_id>` for event-level pairing
- Loads all sub-markets from one event on both venues
- Shows them side-by-side with auto-suggested pairings by `group_item_title`
- Confirm-all-at-once for the common case where event-level structure is clean

---

## STAGE 6 — LLM judge integration

Once discovery is working and you have a reliable candidate set, plug in
an LLM judge as the final binary classifier.

### [ ] Refactor judge interface
- Current pattern: judge sees one pair at a time, returns verdict
- New pattern: judge sees a candidate pair PLUS context from the discovery tool
  (entity overlap, date alignment, sibling list) to inform its decision

### [ ] Confidence-based routing
- High-confidence VALID (>0.95) → auto-accept, save to `cross_pairs.json`
- High-confidence INVALID (<0.05) → auto-reject
- CONDITIONAL, INVERTED, or middle-confidence → human review queue

### [ ] Batch mode for new discoveries
- Run daily: fetch new markets, run twin search on each unpaired one,
  send top candidates to LLM judge
- Pre-populate the human review queue with the LLM's uncertain verdicts
- Human only sees the hard cases; auto-pairs accumulate in the background

### [ ] Cost tracking
- Log token usage per judge call
- Daily cost report
- Hard cap with alert when exceeded

### [ ] LLM verdict audit
- Periodically sample auto-accepted pairs for manual spot-check
- Track LLM accuracy on confirmed-pair settlement outcomes
  (a settled pair that paid $1.00 confirms the LLM was right;
   a pair-mismatch loss means the LLM was wrong)
- Tune confidence thresholds based on observed accuracy

---

## STAGE 7 — Polish and ongoing operations

### [ ] Synonym mapping
Small dictionary for known abbreviations / alternate names:
- "PGA" ↔ "Professional Golf Association"
- "POTUS" ↔ "President", "presidency"
- "BTC" ↔ "Bitcoin"
- Country names: "USA" / "United States" / "US"

### [ ] Search-history cache
Cache results of common searches within a session.
Trivial speedup, nice UX.

### [ ] Score boosting for fielded matches
Hits in `title` weighted 3× hits in `rules_text`.
Already implementable via the field-repetition trick in BM25.

### [ ] Daily refresh pipeline
- Fetch fresh data from both venues
- Rebuild unified index
- Run coverage report
- Run LLM batch judge on new discoveries
- Email/Telegram summary of new auto-paired and new review-queue items

---

## Implementation order recommendation

If you decide to invest in this:

1. **Stage 1 first** (foundation) — the unified index is the prerequisite
   for everything else. Maybe 1-2 days of work.

2. **Stage 4 next** (coverage reports) — tells you what you're currently
   missing. Quick to build on top of the unified index. Half a day.

3. **Stage 2 + 3 together** (multi-signal search + twin search) — the core
   discovery improvement. 2-3 days for solid versions of each search mode,
   plus the twin-search orchestrator.

4. **Stage 5** (workflow integration) — wire it into your existing tool.
   Half a day to a day.

5. **Stage 6** (LLM judge) — only after you've validated the discovery
   layer gives you good candidates. Otherwise the LLM judges noise.

6. **Stage 7** (polish) — ongoing as you find friction points.

Total: maybe 1-2 weeks of focused work. Defer until the cross-venue arb
strategy is producing actual profit and you want to scale coverage.

---

## What this replaces from the current code

- `compute_candidates()` becomes one of several search modes, not the
  only path to candidates
- The embedding cache stays (still useful for one of the signals)
- `cross_pairs.json` schema gets a new `source` field tracking how each
  pair was discovered (embedding vs twin search vs manual lookup vs LLM)
- The `--auto-llm` mode currently using batched Claude/GPT becomes the
  LLM judge in Stage 6, with better context and confidence-based routing

---

## Success metrics

After the upgrade, you should be able to answer:

- "What fraction of Kalshi events have at least one Polymarket pair candidate?"
- "Which uncovered events look most likely to have hidden arb opportunities
  based on volume and category?"
- "When the LLM auto-paired markets last week, what was the realized
  settlement accuracy on those pairs?"

None of those are answerable in the current pipeline.