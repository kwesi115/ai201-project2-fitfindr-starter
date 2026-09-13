# FitFindr — planning.md

> Complete this document before writing any implementation code.
> Your spec and agent diagram are what you'll use to direct AI tools (Claude, Copilot, etc.) to generate your implementation — the more specific they are, the more useful the generated code will be.
> Your planning.md will be reviewed as part of your submission.
> Update it before starting any stretch features.

---

## Tools

List every tool your agent will use. For each tool, fill in all four fields.
You must have at least 3 tools. The three required tools are listed — add any additional tools below them.

**Shared constants (top of `tools.py`):**
- `MODEL = "llama-3.3-70b-versatile"` — Groq model used by every LLM call, via the existing `_get_groq_client()`.
- `COLOR_WORDS: set[str]`: every token that appears in any listing's `colors` field, built once from `load_listings()` (e.g. `black`, `navy`, `tan`, `faded`, `olive`).
- `FIT_CARD_ERROR_PREFIX = "Couldn't create a fit card:"`

**Shared helpers (private, in `tools.py`):**
- `_tokenize(text: str) -> set[str]`: lowercase, `re.findall(r"[a-z0-9]+", ...)`, strip one trailing `s` from tokens longer than 3 characters (`boots` → `boot`, `jeans` → `jean`), then remove stopwords `{a, an, the, for, in, with, and, or, of, to, i, im, looking, want, need, some, something, size, under, me, my}`.
- `_size_matches(user_size: str, listing_size: str) -> bool`: returns `True` if `listing_size` contains `"one size"` (case-insensitive). Otherwise it splits both strings with `re.findall(r"[a-z0-9.]+", s.lower())`, drops the token `us`, and returns `True` only if every user token is in the listing's token set. Results: `"M"` matches `"S/M"` and `"M/L"`, but not `"XL (fits oversized)"`. `"8"` matches `"US 8"` but not `"US 8.5"`. `"W30"` matches `"W30 L30"`.

### Tool 1: search_listings

**What it does:**
Filters the 40 mock listings in `data/listings.json` by price ceiling and size, then ranks the rest by keyword overlap with the description. It's pure Python with no LLM call, so it's deterministic and testable offline.

**Input parameters:**
- `description` (str): the item the user wants, already stripped of price/size phrases (e.g. `"vintage graphic tee"`). Tokenized with `_tokenize`.
- `size` (str | None): the user's size exactly as they wrote it (e.g. `"M"`, `"8"`, `"W30"`). `None` skips size filtering. Compared with `_size_matches`.
- `max_price` (float | None): inclusive price ceiling in USD (`listing["price"] <= max_price`). `None` skips price filtering.

**What it returns:**
`list[dict]`: copies of the matching listing dicts, best match first. Each dict has every original field plus one added field:
- `id` (str, e.g. `"lst_006"`), `title` (str), `description` (str), `category` (str: `tops|bottoms|outerwear|shoes|accessories`), `style_tags` (list[str]), `size` (str), `condition` (str: `excellent|good|fair`), `price` (float), `colors` (list[str]), `brand` (str | None), `platform` (str: `depop|thredUp|poshmark`)
- `match_score` (int): the relevance score computed below.

Scoring, per listing that passes both filters:
1. `title_t = _tokenize(title)`, `tag_t = _tokenize(" ".join(style_tags))`, `other_t = _tokenize(description + category + colors + brand)`.
2. For each query token: +3 if in `title_t`, +2 if in `tag_t`, +1 if in `other_t` (additive).
3. `matched` = query tokens found in any of the three sets. **Drop the listing if `matched` is empty or `matched ⊆ COLOR_WORDS`.** A color-only match (e.g. "black" matching Biker Shorts for a "black combat boots" query) doesn't count.
4. Sort by `(-match_score, price)`: highest score first, and the cheaper listing wins a tie.

Verified against the dataset: `("vintage graphic tee", None, 30.0)` returns 20 listings, top 3 = `lst_006` (15), `lst_033` (14), `lst_015` (12). `("track jacket", "M", None)` returns top `lst_004` (8). `("black combat boots", "8", None)` returns `[]`.

**What happens if it fails or returns nothing:**
The tool never raises for no matches. It returns `[]`. The planning loop (Step 4) then runs **one** relaxed search, `search_listings(description, None, None)`, if the user set a size or price. It uses that to tell the user which filter excluded the closest item, then stops **without** calling `suggest_outfit`. If `listings.json` is missing or unreadable, `load_listings()` raises `FileNotFoundError`/`json.JSONDecodeError`. That's a setup bug, not a user error, so the exception is not caught.

---

### Tool 2: suggest_outfit

**What it does:**
Sends the selected listing and the user's wardrobe to the Groq LLM and returns 1–2 complete outfits built from the new item plus pieces the user already owns, named exactly as they appear in the wardrobe. If the wardrobe is empty, it returns general styling advice for the item instead.

**Input parameters:**
- `new_item` (dict): one listing dict from `search_listings` (the session's `selected_item`). Fields used in the prompt: `title`, `category`, `colors`, `style_tags`, `description`.
- `wardrobe` (dict): `{"items": [ {id, name, category, colors, style_tags, notes}, ... ]}` per `data/wardrobe_schema.json`. Items are read with `wardrobe.get("items") or []`, so a missing key or `[]` both count as empty.

**What it returns:**
`str`, always non-empty. Two modes:
- **Wardrobe has items:** the prompt lists each item as `- {name} ({category}; colors: {colors}; tags: {style_tags}; notes: {notes or "none"})` and asks for exactly this format, at temperature 0.7 with max_tokens 400:
  ```
  Outfit 1: <new item title> + <wardrobe name> + <wardrobe name> [+ ...] — <one sentence on why it works>
  Outfit 2: <new item title> + <wardrobe name> + <wardrobe name> [+ ...] — <one sentence on why it works>
  ```
  Each outfit uses 2–4 wardrobe pieces, copied verbatim from `name`, and must not repeat the new item's category (no second top with a top).
- **Wardrobe empty:** the prompt asks for 3–4 sentences covering which item types pair well (bottoms, shoes, layers), which colors to pair with its `colors`, and what vibe it suits. The response is returned with the prefix `"You haven't added any wardrobe pieces yet, so here are general ways to style it: "`.

**What happens if it fails or returns nothing:**
- **Empty wardrobe:** handled as the second mode above. This is **not** an error, and the agent continues to `create_fit_card`.
- **LLM returns empty/whitespace text:** the tool returns a deterministic fallback with no second LLM call. With a wardrobe it's `"Outfit 1: {title} + {first bottoms item name} + {first shoes item name} — an easy base to build on."`, skipping any category the wardrobe lacks. With an empty wardrobe it's `"You haven't added any wardrobe pieces yet, so here are general ways to style it: pair the {title} with simple {first style_tag} basics in neutral colors."`
- **Groq raises** (`ValueError` from a missing `GROQ_API_KEY`, `groq.RateLimitError`, `groq.APIConnectionError`, `groq.APIError`): the tool does **not** catch it. The planning loop (Step 6) catches it, sets `session["error"]` to the matching message from the Error Handling table, and returns early.

---

### Tool 3: create_fit_card

**What it does:**
Turns the selected listing and its outfit suggestion into a 2–4 sentence casual Instagram/TikTok caption. The caption mentions the item, its price, and the platform once each, plus at least one specific piece from the outfit.

**Input parameters:**
- `outfit` (str): the `outfit_suggestion` string returned by `suggest_outfit`.
- `new_item` (dict): the same listing dict passed to `suggest_outfit`. Required keys: `title`, `price`, `platform`. `condition` is used if present.

**What it returns:**
`str`, the caption. Prompt rules (temperature 1.0 so repeated runs differ, max_tokens 150):
- 2–4 sentences, lowercase, first-person OOTD voice. Not a product description.
- Mentions a short form of the title (the part before ` — `, e.g. "2003 tour bootleg tee"), the price as `$24`, and the platform name, **once each**.
- Names at least one wardrobe piece or pairing from `outfit`.
- At most 2 emoji and at most 2 hashtags.

Example: `"grabbed this 2003 tour bootleg tee off depop for $24 and it's giving 90s basement show 🖤 boxy fit half-tucked into my baggy dark-wash jeans with the chunky white sneakers. might never take it off"`

**What happens if it fails or returns nothing:**
These guards run **before** creating the Groq client, so they work without an API key. Neither one raises.
- `outfit` is `None` or `outfit.strip() == ""` → return `"Couldn't create a fit card: no outfit suggestion was provided for this item."`
- `new_item` is missing `title`, `price`, or `platform` → return `"Couldn't create a fit card: the listing is missing {', '.join(missing_keys)}."`
- **LLM returns empty text:** return the template caption `f"thrifted this {short_title.lower()} off {platform} for ${price:.0f} and it already has a spot in my rotation."`
- **Groq raises:** the tool does not catch it. The planning loop (Step 7) catches it, uses the same template caption (via `_template_caption(new_item)`), and adds a warning. The user still gets a fit card because the listing and outfit are already valid.

---

### Additional Tools (if any)

### Tool 4: parse_query

**What it does:**
Converts the raw natural-language query into the three `search_listings` arguments. It asks Groq for strict JSON and falls back to a regex parser if the LLM call fails or returns invalid JSON. It uses an LLM first because queries mix what the user wants with what they already own ("I mostly wear baggy jeans…"), and only the wanted item belongs in `description`.

**Input parameters:**
- `query` (str): the user's full message, e.g. `"I'm looking for a vintage graphic tee under $30. I mostly wear baggy jeans and chunky sneakers. What's out there and how would I style it?"`

**What it returns:**
`tuple[dict, str]` = `(parsed, method)`:
- `parsed = {"description": str, "size": str | None, "max_price": float | None}`. `description` is lowercase, only the item wanted, with no price/size words. `max_price` is cast to `float`.
- `method` is `"llm"` or `"regex"`.

LLM call: temperature 0, `response_format={"type": "json_object"}`. The system prompt says: *"Extract the ONE clothing item the user wants to buy. Return JSON with keys description (short lowercase item phrase, no price or size words, exclude items they already own), size (string exactly as written, or null), max_price (number, or null)."*

The result is valid only if it's a dict with a non-empty string `description`, `size` that is a string or null, and `max_price` that is a positive number or null. Anything else goes to the regex fallback.

Regex fallback (`_regex_parse`), applied to the first sentence only (`re.split(r"[.?!]", query, maxsplit=1)[0]`):
- price: `(?:under|below|less than|max|up to)\s*\$?\s*(\d+(?:\.\d+)?)|\$(\d+(?:\.\d+)?)` (case-insensitive)
- size: `\bsize\s+((?:us\s+)?[a-z0-9./]+)` (case-insensitive), else bare `\b(XXS|XS|S|M|L|XL|XXL)\b` (case-sensitive, so the "m" in "I'm" doesn't match). The size is uppercased.
- description: remove the matched price and size text, remove filler `\b(i'?m|i am|looking for|i want|i need|find me|show me|a|an|some|in)\b`, replace punctuation with spaces, collapse whitespace, lowercase.

Verified regex output: `"90s track jacket in size M"` → `{"description": "90s track jacket", "size": "M", "max_price": None}`. `"designer ballgown size XXS under $5"` → `{"description": "designer ballgown", "size": "XXS", "max_price": 5.0}`. The full example query above → `{"description": "vintage graphic tee", "size": None, "max_price": 30.0}`.

**What happens if it fails or returns nothing:**
- **Groq raises** (including a missing API key) or returns invalid/incomplete JSON → use `_regex_parse(query)` and return `method="regex"`. The user sees no message; the loop only records a warning.
- **Both parsers produce `description == ""`** (e.g. query `"under $20 size M"`) → the planning loop (Step 2) sets an error asking the user to name the item and returns early.

---

## Planning Loop

**How does your agent decide which tool to call next?**

`run_agent(query, wardrobe)` runs a fixed sequence with conditional early exits. It makes one pass with no open-ended re-planning. Every branch below either continues to the next numbered step or sets `session["error"]` and executes `return session`. The worst case is 3 LLM calls (parse, outfit, caption) and 2 `search_listings` calls.

**Step 1 — Initialize and guard the query.**
Call `session = _new_session(query, wardrobe)`. Check `query is None or query.strip() == ""`.
- If true: set `session["error"] = "Tell me what you're looking for — for example: \"vintage graphic tee under $30, size M\"."` and `return session`.
- Otherwise, proceed to Step 2.

**Step 2 — Parse.**
Call `parsed, method = parse_query(query)`. Set `session["parsed"] = parsed` and `session["parse_method"] = method`. If `method == "regex"`, append `"Query parsed with regex fallback (LLM parser unavailable)."` to `session["warnings"]`.
Then check `parsed["description"] == ""`.
- If true: set `session["error"] = f"I couldn't tell what item you want from \"{query}\". Name the piece — for example \"graphic tee\", \"cargo pants\", or \"chelsea boots\" — and add a size or price if you like."` and `return session`.
- Otherwise, proceed to Step 3.

**Step 3 — Search.**
Call `results = search_listings(parsed["description"], parsed["size"], parsed["max_price"])`. Set `session["search_results"] = results`.
- If `len(results) > 0`: proceed to Step 5.
- If `results == []`: proceed to Step 4.

**Step 4 — No-results recovery. Every branch here returns, and `suggest_outfit` is never called.**
- **4a.** If `parsed["size"] is None and parsed["max_price"] is None`, filters weren't the cause. Set `session["error"] = f"No listings matched \"{description}\". Try a broader word for the item (e.g. \"dress\", \"blazer\", \"boots\") — we carry tops, bottoms, outerwear, shoes, and accessories."` and `return session`.
- **4b.** Otherwise, call `relaxed = search_listings(parsed["description"], None, None)` and set `session["relaxed_results"] = relaxed`. If `relaxed == []`: set `session["error"] = f"No listings matched \"{description}\" at any size or price, so loosening your filters won't help. Try a broader word for the item (e.g. \"dress\", \"blazer\", \"boots\")."` and `return session`.
- **4c.** Otherwise, set `best = relaxed[0]`, `size_blocked = parsed["size"] is not None and not _size_matches(parsed["size"], best["size"])`, and `price_blocked = parsed["max_price"] is not None and best["price"] > parsed["max_price"]`. Build the message:
  - `f"Nothing matched \"{description}\"{filter_text}. The closest listing without your filters is {best['title']} — ${best['price']:.2f}, size {best['size']}, on {best['platform']}."`. `filter_text` is `" in size {size}"` and/or `" under ${max_price:.0f}"`, whichever were set.
  - If `size_blocked`, append `f" It's listed as size {best['size']}, not {parsed['size']}."`
  - If `price_blocked`, append `f" It's ${best['price'] - parsed['max_price']:.2f} over your budget."`
  - Append `" Search again without those filters to see it."`

  Set `session["error"]` to that message and `return session`. The near-miss is **not** passed to `suggest_outfit`, because it isn't what the user asked for.

**Step 5 — Select.**
Set `session["selected_item"] = results[0]`. The ranking and tie-break (score desc, then price asc) already happened inside `search_listings`, so no LLM re-ranking is needed. Proceed to Step 6.

**Step 6 — Outfit.**
Call `outfit = suggest_outfit(session["selected_item"], session["wardrobe"])` inside `try`:
- `except ValueError` (missing `GROQ_API_KEY`): set `session["error"]` to the missing-key message from the Error Handling table and `return session`.
- `except groq.RateLimitError`, `except groq.APIConnectionError`, `except groq.APIError`: set `session["error"]` to the matching table message and `return session`. `selected_item` stays set for debugging, but the UI shows only the error.
- On success: set `session["outfit_suggestion"] = outfit` and proceed to Step 7. The loop does **not** branch on an empty wardrobe; `suggest_outfit` handles that and still returns a string.

**Step 7 — Fit card.**
Call `card = create_fit_card(session["outfit_suggestion"], session["selected_item"])` inside `try`:
- `except groq.APIError as e` (includes the RateLimitError/APIConnectionError subclasses): set `card = _template_caption(session["selected_item"])` and append `f"Fit card used template caption ({type(e).__name__})."` to `session["warnings"]`. **Continue; do not return an error.**

After the `try`, check `card.startswith(FIT_CARD_ERROR_PREFIX)`. This happens only if the listing is missing `title`/`price`/`platform`.
- If true: set `session["error"] = card` and `session["outfit_suggestion"] = None` (this keeps the contract that outfit and fit card are `None` whenever there's an error) and `return session`.
- Otherwise: set `session["fit_card"] = card` and proceed to Step 8.

**Step 8 — Done.**
`return session` with `session["error"] is None`. The agent knows it's done when `fit_card` is set. There are no further tool calls.

---

## State Management

**How does information from one tool get passed to the next?**

All state for one interaction lives in the single `session` dict created by `_new_session()` in `agent.py`. The tools never see the session. They are pure functions that receive plain values. Only `run_agent` reads a value out of the session, passes it into the next tool, and writes the tool's result back. A new session is created for every query, and nothing persists between queries.

| Key | Type | Written by (step) | Read by |
|-----|------|-------------------|---------|
| `query` | str | `_new_session` (1) | `parse_query` (2), error messages |
| `wardrobe` | dict | `_new_session` (1) | `suggest_outfit` (6) |
| `parsed` | dict `{description, size, max_price}` | Step 2 | `search_listings` (3, 4b), error messages (4) |
| `parse_method` | `"llm"` \| `"regex"` | Step 2 | debugging only |
| `search_results` | list[dict] | Step 3 | Step 5, `app.py` ("1 of N matches") |
| `relaxed_results` | list[dict] | Step 4b | near-miss message (4c) |
| `selected_item` | dict \| None | Step 5 | `suggest_outfit` (6), `create_fit_card` (7), `app.py` listing panel |
| `outfit_suggestion` | str \| None | Step 6 | `create_fit_card` (7), `app.py` outfit panel |
| `fit_card` | str \| None | Step 7 | `app.py` fit card panel |
| `warnings` | list[str] | Steps 2, 7 | printed in CLI, not shown in UI |
| `error` | str \| None | Steps 1, 2, 4, 6, 7 | `app.py`: if set, shown in panel 1, panels 2–3 empty |

New keys to add to `_new_session()`: `parse_method: None`, `relaxed_results: []`, `warnings: []`.

`app.py`'s `handle_query` formats the listing panel from `selected_item` as:
```
{title}
${price:.2f} · {platform} · size {size} · {condition} condition
{description}
(best of {len(search_results)} matches)
```

---

## Error Handling

For each tool, describe the specific failure mode you're handling and what the agent does in response.

| Tool | Failure mode | Agent response |
|------|-------------|----------------|
| (planning loop) | Query is empty or whitespace | Ends at Step 1, no tools called. Shows: *"Tell me what you're looking for — for example: "vintage graphic tee under $30, size M"."* |
| parse_query | Groq call fails or returns invalid JSON | No user-facing message. Falls back to `_regex_parse`, sets `parse_method="regex"`, adds a warning, and continues to search. |
| parse_query | No item description could be extracted (e.g. "under $20 size M") | Ends at Step 2. Shows: *"I couldn't tell what item you want from "under $20 size M". Name the piece — for example "graphic tee", "cargo pants", or "chelsea boots" — and add a size or price if you like."* |
| search_listings | No results match the query, and no size/price filter was set | Ends at Step 4a, outfit tool not called. Shows: *"No listings matched "{description}". Try a broader word for the item (e.g. "dress", "blazer", "boots") — we carry tops, bottoms, outerwear, shoes, and accessories."* |
| search_listings | No results match the query because of the size/price filter, and a relaxed search finds something | Ends at Step 4c. Offers the closest listing. For "black combat boots size 8": *"Nothing matched "black combat boots" in size 8. The closest listing without your filters is Suede Chelsea Boots — Tan — $44.00, size US 8.5, on poshmark. It's listed as size US 8.5, not 8. Search again without those filters to see it."* |
| search_listings | No results match the query, even with filters removed | Ends at Step 4b. For "designer ballgown size XXS under $5": *"No listings matched "designer ballgown" at any size or price, so loosening your filters won't help. Try a broader word for the item (e.g. "dress", "blazer", "boots")."* |
| suggest_outfit | Wardrobe is empty | **Not an error.** The tool returns general styling advice starting *"You haven't added any wardrobe pieces yet, so here are general ways to style it: …"*. The agent continues to `create_fit_card`, and all three panels are filled. |
| suggest_outfit | `GROQ_API_KEY` not set (`ValueError`) | Ends at Step 6. Shows: *"Found {title} (${price:.2f} on {platform}), but outfit ideas need the Groq API and GROQ_API_KEY isn't set. Add GROQ_API_KEY=your_key to a .env file in the project root and restart the app."* |
| suggest_outfit | Groq rate limit (`groq.RateLimitError`) | Ends at Step 6. Shows: *"Found {title} (${price:.2f} on {platform}), but Groq's rate limit was hit while generating outfit ideas. Wait about 30 seconds and search again."* |
| suggest_outfit | Network failure (`groq.APIConnectionError`) or other `groq.APIError` | Ends at Step 6. Shows: *"Found {title} (${price:.2f} on {platform}), but I couldn't reach the outfit generator ({ExceptionName}). Check your internet connection and search again."* |
| suggest_outfit | LLM returns empty text | No error. The tool returns a deterministic outfit line built from the first bottoms and shoes items in the wardrobe, and the agent continues. |
| create_fit_card | Outfit input is missing or incomplete | The tool returns *"Couldn't create a fit card: no outfit suggestion was provided for this item."* without calling the LLM. The agent can't reach this, because Step 6 always sets a non-empty outfit or returns first. It's covered by a direct tool test. |
| create_fit_card | Listing dict missing `title`/`price`/`platform` | The tool returns *"Couldn't create a fit card: the listing is missing price."* (keys listed). Step 7 sees the prefix, sets it as `session["error"]`, clears `outfit_suggestion`, and returns. |
| create_fit_card | Groq raises, or returns empty text | Degrades instead of ending. The fit card panel shows the template caption *"thrifted this graphic tee off depop for $24 and it already has a spot in my rotation."* A warning is recorded, and the listing and outfit panels are unaffected. |

---

## Architecture

Every `[ERROR]` branch sets `session["error"]` and follows the right-hand rail to `return session`. `~>` marks a degraded path that keeps going.

```
User query (str)  +  wardrobe choice ("Example" | "Empty")
    │  app.py handle_query → run_agent(query, wardrobe)
    ▼
Planning Loop (agent.py) ─────────────────────────────────────────────────────────────┐
    │                                                                                 │
    │ Session: query, wardrobe, error=None, warnings=[]                               │
    │ query.strip() == ""                                                             │
    ├──► [ERROR] "Tell me what you're looking for — for example ..." ─────────────────┤
    │                                                                                 │
    ├─► parse_query(query)                                                            │
    │       │ Groq JSON (temp 0) fails / invalid                                      │
    │       ├~> _regex_parse(query); warnings += "regex fallback"                     │
    │       │ parsed.description == ""                                                │
    │       ├──► [ERROR] "I couldn't tell what item you want ..." ────────────────────┤
    │       │ parsed = {description, size, max_price}                                 │
    │       ▼                                                                         │
    │   Session: parsed = {...}, parse_method = "llm" | "regex"                       │
    │       │                                                                         │
    ├─► search_listings(description, size, max_price)                                 │
    │       │ results == [] and no size/price set                                     │
    │       ├──► [ERROR] "No listings matched ..." ───────────────────────────────────┤
    │       │ results == [] and size/price set                                        │
    │       ├──► search_listings(description, None, None) → relaxed_results           │
    │       │       │ relaxed == []                                                   │
    │       │       ├──► [ERROR] "... at any size or price ..." ──────────────────────┤
    │       │       │ relaxed != []                                                   │
    │       │       └──► [ERROR] "Nothing matched ... closest is <title> $<price>" ───┤
    │       │ results = [listing + match_score, ...] (score desc, price asc)          │
    │       ▼                                                                         │
    │   Session: search_results = results, selected_item = results[0]                 │
    │       │                                                                         │
    ├─► suggest_outfit(selected_item, wardrobe)                                       │
    │       │ raises ValueError / RateLimitError / APIConnectionError / APIError      │
    │       ├──► [ERROR] "Found <title> ($<price> on <platform>), but ..." ───────────┤
    │       │ wardrobe.items == [] → general styling text (continues, not an error)   │
    │       │ outfit text (str)                                                       │
    │       ▼                                                                         │
    │   Session: outfit_suggestion = text                                             │
    │       │                                                                         │
    └─► create_fit_card(outfit_suggestion, selected_item)                             │
            │ raises groq.APIError                                                    │
            ├~> _template_caption(selected_item); warnings += "template caption"      │
            │ returns "Couldn't create a fit card: ..."                               │
            ├──► [ERROR] session.error = that string; outfit_suggestion = None ───────┤
            │ caption (str)                                                           │
            ▼                                                                         │
        Session: fit_card = caption                                                   │
            │                                                                         │
            │                                         error path: return session ─────┘
            ▼                                         (error set; outfit, fit_card None)
        Return session
            │  app.py: error? → (error, "", "")
            ▼          else  → (listing_text, outfit_suggestion, fit_card)
        3 UI panels: 🛍️ listing | 👗 outfit idea | ✨ fit card
```

---

## AI Tool Plan

**AI tool:** Claude Code, run in this repo so it can read `tools.py`, `agent.py`, `utils/data_loader.py`, and `data/*.json` directly. Each prompt names the exact planning.md section to follow and tells it not to change function signatures.

**Milestone 3 — Individual tool implementations:**

- **`search_listings`**
  - **Input:** the "Shared constants", "Shared helpers", and "Tool 1: search_listings" blocks. I'll ask Claude to implement `search_listings`, `_tokenize`, `_size_matches`, and `COLOR_WORDS` in `tools.py` using `load_listings()` from `utils/data_loader.py`.
  - **Expected output:** code in `tools.py` only. No Groq call in this function.
  - **Checks before running:**
    1. Price and size filters run before scoring.
    2. The price comparison is `<=`.
    3. The color-only drop rule uses `matched <= COLOR_WORDS`.
    4. The sort key is `(-match_score, price)`.
    5. It returns listing-dict copies with `match_score`, not `(score, dict)` tuples.
    6. It never mutates the loaded listings.
  - **Tests** (`tests/test_tools.py`, 5 queries, run offline):
    - `("vintage graphic tee", None, 30.0)` → 20 results, `[0]["id"] == "lst_006"`, all prices ≤ 30.
    - `("track jacket", "M", None)` → `[0]["id"] == "lst_004"`.
    - `("black combat boots", "8", None)` → `[]`.
    - `("black combat boots", None, None)` → exactly `["lst_028"]`.
    - `("designer ballgown", "XXS", 5.0)` → `[]`.
- **`parse_query`**
  - **Input:** the "Tool 4: parse_query" block, with the regex patterns copied verbatim.
  - **Checks before running:** the JSON validation rejects a missing/empty `description` and a non-positive `max_price`, and every exception path goes to `_regex_parse`, not a bare `except: pass`.
  - **Tests:**
    - Call `_regex_parse` directly on the 6 queries from `app.py`'s `EXAMPLE_QUERIES` plus the long walkthrough query. Assert the exact dicts listed in the Tool 4 block.
    - Run `parse_query` with `GROQ_API_KEY` unset and assert `method == "regex"`.
    - Run it once with the key set on the walkthrough query and assert `description == "vintage graphic tee"` (it must not include "baggy jeans").
- **`suggest_outfit`**
  - **Input:** the "Tool 2: suggest_outfit" block plus the `schema` section of `data/wardrobe_schema.json`. I'll ask Claude to use `_get_groq_client()` and `MODEL`.
  - **Checks before running:**
    1. It branches on `wardrobe.get("items") or []`.
    2. It does **not** wrap the Groq call in try/except.
    3. Temperature is 0.7.
    4. The empty-response fallback exists.
    5. Wardrobe items are formatted exactly as `- {name} ({category}; colors: ...; tags: ...; notes: ...)`.
  - **Tests** (3 live calls):
    - `lst_006` + example wardrobe → contains `"Outfit 1:"`, and at least 2 wardrobe `name` strings appear verbatim.
    - `lst_006` + empty wardrobe → starts with `"You haven't added any wardrobe pieces yet"`.
    - `lst_013` (slip dress) + example wardrobe → no outfit line includes a second `bottoms` wardrobe item.
- **`create_fit_card`**
  - **Input:** the "Tool 3: create_fit_card" block.
  - **Checks before running:** both guards run before `_get_groq_client()` is called, temperature is 1.0, and the error strings start with `FIT_CARD_ERROR_PREFIX`.
  - **Tests** (4 calls):
    - `("", lst_006)` → the exact no-outfit string, with no API key needed.
    - `(outfit, lst_006 without "price")` → `"...missing price."`.
    - A valid outfit + `lst_006` → the caption contains `"$24"` and `"depop"` (case-insensitive) and has 2–4 sentences.
    - The same valid input run twice → the two captions are not identical.

**Milestone 4 — Planning loop and state management:**

- **Input:** give Claude Code the "Planning Loop", "State Management", "Error Handling", and "Architecture" sections of this file plus the current `agent.py`. Ask it to implement `run_agent` step by step exactly as numbered, add `parse_method`, `relaxed_results`, and `warnings` to `_new_session()`, and add `_template_caption` to `tools.py`.
- **Expected output:** changes to `agent.py` and one helper in `tools.py`, with no changes to tool signatures.
- **Checks before running:**
  1. Every `[ERROR]` branch in the diagram has a matching `session["error"] = ...` followed by `return session`. That's 7 early returns: Steps 1, 2, 4a, 4b, 4c, 6, and 7.
  2. `suggest_outfit` is unreachable when `search_results == []`.
  3. The error strings match the Error Handling table word-for-word.
  4. Step 7's exception handler does **not** set `session["error"]`.
- **Tests:**
  - Run `python agent.py`. The built-in happy path must print a title, and the "designer ballgown" case must print the Step 4b message.
  - Add 4 pytest cases in `tests/test_agent.py`:
    - `"black combat boots size 8"` → `error` contains `"Suede Chelsea Boots"` and `outfit_suggestion is None`.
    - `"   "` → the Step 1 message, and `search_results == []`.
    - `"vintage graphic tee under $30"` with `get_empty_wardrobe()` → `error is None`, and the outfit starts with the general-advice prefix.
    - Monkeypatch `agent.suggest_outfit` to raise `groq.APIConnectionError` → `error` starts with `"Found Graphic Tee — 2003 Tour Bootleg Style ($24.00 on depop)"`.

**Milestone 5 — Gradio `handle_query`:**

- **Input:** give Claude Code the State Management listing-panel format and the `app.py` TODO.
- **Checks before running:** an empty query returns before `run_agent` is called, and the error path returns exactly `(session["error"], "", "")`.
- **Tests:** run `python app.py` and click all 5 example queries. Graphic tee, track jacket, and midi skirt fill all three panels. "black combat boots size 8" and "designer ballgown" show only the error panel.

---

## A Complete Interaction (Step by Step)

Write out what a full user interaction looks like from start to finish — tool call by tool call. Use a specific example query.

**Example user query:** "I'm looking for a vintage graphic tee under $30. I mostly wear baggy jeans and chunky sneakers. What's out there and how would I style it?" (Wardrobe radio: **Example wardrobe**.)

**Step 1:**
`query.strip()` is non-empty, so the loop calls `parse_query(query)`. Groq (temp 0, JSON mode) returns `{"description": "vintage graphic tee", "size": null, "max_price": 30}`, which passes validation. Session: `parsed = {"description": "vintage graphic tee", "size": None, "max_price": 30.0}`, `parse_method = "llm"`.
- `size` is `None` because the user never gave a size.
- "baggy jeans and chunky sneakers" is excluded from `description`, because those are things the user already owns. They reach the agent through the wardrobe, not the search.

**Step 2:**
The loop calls `search_listings("vintage graphic tee", None, 30.0)`. After the price filter and scoring, 20 listings remain. The top 3:

| # | id | title | price | size | platform | condition | match_score |
|---|----|-------|-------|------|----------|-----------|-------------|
| 1 | lst_006 | Graphic Tee — 2003 Tour Bootleg Style | $24.00 | L | depop | good | 15 |
| 2 | lst_033 | Vintage Band Tee — Faded Grey | $19.00 | L | depop | fair | 14 |
| 3 | lst_015 | Vintage Graphic Hoodie — Faded Black | $26.00 | L | depop | fair | 12 |

`lst_006` scores 15 = "vintage" (tags +2, description +1) + "graphic" (title +3, tags +2, description +1) + "tee" (title +3, tags +2, description +1). `results != []`, so Step 4 is skipped. Session: `search_results = <20 listings>`, `selected_item = lst_006`.

**Step 3:**
The loop calls `suggest_outfit(new_item=lst_006, wardrobe=<example wardrobe, 10 items>)`. The wardrobe is non-empty, so the prompt lists all 10 pieces. Example LLM output (the wording varies run to run):
```
Outfit 1: Graphic Tee — 2003 Tour Bootleg Style + Baggy straight-leg jeans, dark wash + Chunky white sneakers + Black crossbody bag — the boxy black tee half-tucked into high-waisted baggy denim keeps the streetwear proportions balanced, and the white sneakers break up all the dark tones.
Outfit 2: Graphic Tee — 2003 Tour Bootleg Style + Wide-leg khaki trousers + Black combat boots + Vintage black denim jacket — the cropped black denim jacket and combat boots lean into the tee's grunge side, while the khaki wide-legs keep it from reading all-black.
```
Session: `outfit_suggestion = <that text>`.

**Step 4:**
The loop calls `create_fit_card(outfit=<Step 3 text>, new_item=lst_006)`. Both guards pass (the outfit is non-empty, and `title`/`price`/`platform` are present). Groq (temp 1.0) returns a caption that doesn't start with `FIT_CARD_ERROR_PREFIX`. Session: `fit_card = <caption>`, `error = None`, `warnings = []`. `run_agent` returns the session.

**Final output to user:**
`handle_query` sees `session["error"] is None` and fills the three panels:

- **🛍️ Top listing found**
  ```
  Graphic Tee — 2003 Tour Bootleg Style
  $24.00 · depop · size L · good condition
  Vintage-style bootleg tee with faded graphic. Slightly boxy fit. 100% cotton, soft and worn-in.
  (best of 20 matches)
  ```
- **👗 Outfit idea**: the two `Outfit 1:` / `Outfit 2:` lines from Step 3.
- **✨ Your fit card**: *"grabbed this 2003 tour bootleg tee off depop for $24 and it's giving 90s basement show 🖤 boxy fit half-tucked into my baggy dark-wash jeans with the chunky white sneakers. might never take it off"*

**Contrast (early exit):** with the same example wardrobe, the query "black combat boots size 8" parses to `{"description": "black combat boots", "size": "8", "max_price": None}`.
1. `search_listings` returns `[]`.
2. Because a size was set, the loop runs `search_listings("black combat boots", None, None)`, which returns `[lst_028]`.
3. `_size_matches("8", "US 8.5")` is `False`.

The user sees only the Step 4c message ("…closest listing without your filters is Suede Chelsea Boots — Tan — $44.00, size US 8.5, on poshmark. It's listed as size US 8.5, not 8…"). `suggest_outfit` and `create_fit_card` are never called.
