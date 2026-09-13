"""
tools.py

The three required FitFindr tools. Each tool is a standalone function that
can be called and tested independently before being wired into the agent loop.

Complete and test each tool before moving to agent.py.

Tools:
    search_listings(description, size, max_price)  → list[dict]
    suggest_outfit(new_item, wardrobe)              → str
    create_fit_card(outfit, new_item)               → str
    parse_query(query)                              → tuple[dict, str]
"""

import json
import os
import re

from dotenv import load_dotenv
from groq import APIError, Groq

from utils.data_loader import load_listings

load_dotenv()


# ── Groq client ───────────────────────────────────────────────────────────────

# No Llama chat models are available to this project's Groq key, so default to
# gpt-oss-120b. Set GROQ_MODEL in .env to use a different model.
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")


def _get_groq_client():
    """Initialize and return a Groq client using GROQ_API_KEY from .env."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise ValueError(
            "GROQ_API_KEY not set. Add it to a .env file in the project root."
        )
    return Groq(api_key=api_key)


# ── search helpers ────────────────────────────────────────────────────────────

_STOPWORDS = {
    "a", "an", "the", "for", "in", "with", "and", "or", "of", "to", "i", "im",
    "looking", "want", "need", "some", "something", "size", "under", "me", "my",
}


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokens with a naive plural strip ("boots" -> "boot"), minus stopwords."""
    tokens = set()
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        if len(tok) > 3 and tok.endswith("s"):
            tok = tok[:-1]
        tokens.add(tok)
    return tokens - _STOPWORDS


def _size_matches(user_size: str, listing_size: str) -> bool:
    """
    True if the listing fits the user's size. "One size" listings fit everyone;
    otherwise every token of the user's size must appear in the listing's size
    ("M" matches "S/M", "8" matches "US 8" but not "US 8.5").
    """
    if "one size" in listing_size.lower():
        return True
    user_tokens = set(re.findall(r"[a-z0-9.]+", user_size.lower())) - {"us"}
    listing_tokens = set(re.findall(r"[a-z0-9.]+", listing_size.lower())) - {"us"}
    return user_tokens <= listing_tokens


# Every word used in a listing's colors. A listing whose only matches are color
# words (e.g. "black" for "black combat boots") is not a relevant match.
COLOR_WORDS = set().union(
    *(_tokenize(" ".join(listing["colors"])) for listing in load_listings())
)


# ── Tool 1: search_listings ───────────────────────────────────────────────────

def search_listings(
    description: str,
    size: str | None = None,
    max_price: float | None = None,
) -> list[dict]:
    """
    Search the mock listings dataset for items matching the description,
    optional size, and optional price ceiling.

    Args:
        description: Keywords describing what the user is looking for
                     (e.g., "vintage graphic tee").
        size:        Size string to filter by, or None to skip size filtering.
                     Matching is case-insensitive (e.g., "M" matches "S/M").
        max_price:   Maximum price (inclusive), or None to skip price filtering.

    Returns:
        A list of matching listing dicts, sorted by relevance (best match first,
        cheaper listing first on a tie). Returns an empty list if nothing
        matches — does NOT raise an exception.

    Each listing dict has the following fields:
        id, title, description, category, style_tags (list), size,
        condition, price (float), colors (list), brand, platform,
        match_score (int) — added by this function

    Scoring: each query token scores +3 if it appears in the title, +2 in
    style_tags, +1 in description/category/colors/brand. Listings that match
    no query tokens, or only color words, are dropped.
    """
    query_tokens = _tokenize(description)
    results = []

    for listing in load_listings():
        if max_price is not None and listing["price"] > max_price:
            continue
        if size and not _size_matches(size, listing["size"]):
            continue

        title_tokens = _tokenize(listing["title"])
        tag_tokens = _tokenize(" ".join(listing["style_tags"]))
        other_tokens = _tokenize(" ".join([
            listing["description"],
            listing["category"],
            " ".join(listing["colors"]),
            listing["brand"] or "",
        ]))

        matched = {
            tok for tok in query_tokens
            if tok in title_tokens or tok in tag_tokens or tok in other_tokens
        }
        if not matched or matched <= COLOR_WORDS:
            continue

        score = sum(
            3 * (tok in title_tokens) + 2 * (tok in tag_tokens) + (tok in other_tokens)
            for tok in query_tokens
        )
        results.append({**listing, "match_score": score})

    results.sort(key=lambda item: (-item["match_score"], item["price"]))
    return results


# ── LLM helper ────────────────────────────────────────────────────────────────

def _chat(
    prompt: str,
    temperature: float,
    max_tokens: int,
    system: str | None = None,
    json_mode: bool = False,
) -> str:
    """
    Send one user message (plus an optional system prompt) to MODEL and return
    the stripped reply ("" if empty). json_mode asks Groq for a JSON object.
    Groq exceptions (and ValueError for a missing API key) propagate to the caller.
    """
    client = _get_groq_client()
    messages = [{"role": "user", "content": prompt}]
    if system:
        messages.insert(0, {"role": "system", "content": system})

    options = {}
    if json_mode:
        options["response_format"] = {"type": "json_object"}
    # gpt-oss models reason before answering; "low" keeps the hidden reasoning
    # from using up max_tokens and leaving the visible reply empty.
    if MODEL.startswith("openai/gpt-oss"):
        options["extra_body"] = {"reasoning_effort": "low"}

    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        **options,
    )
    return (response.choices[0].message.content or "").strip()


# ── Tool 2: suggest_outfit ────────────────────────────────────────────────────

EMPTY_WARDROBE_PREFIX = (
    "You haven't added any wardrobe pieces yet, so here are general ways to style it: "
)


def _fallback_outfit(new_item: dict, items: list[dict]) -> str:
    """Deterministic outfit line used when the LLM returns no text."""
    pieces = []
    for category in ("bottoms", "shoes"):
        if category == new_item["category"]:
            continue
        match = next((item for item in items if item["category"] == category), None)
        if match:
            pieces.append(match["name"])
    if not pieces:
        pieces = [items[0]["name"]]
    return f"Outfit 1: {new_item['title']} + {' + '.join(pieces)} — an easy base to build on."


def suggest_outfit(new_item: dict, wardrobe: dict) -> str:
    """
    Given a thrifted item and the user's wardrobe, suggest 1–2 complete outfits.

    Args:
        new_item: A listing dict (the item the user is considering buying).
        wardrobe: A wardrobe dict with an 'items' key containing a list of
                  wardrobe item dicts. May be empty — handle this gracefully.

    Returns:
        A non-empty string with outfit suggestions, one per line, formatted as
        "Outfit N: <item title> + <wardrobe piece> + ... — <why it works>".
        If the wardrobe is empty, returns general styling advice prefixed with
        EMPTY_WARDROBE_PREFIX rather than raising or returning an empty string.
        If the LLM returns no text, returns a deterministic fallback suggestion.

    Raises:
        ValueError if GROQ_API_KEY is missing, or a groq.APIError subclass if the
        API call fails — the planning loop turns these into user-facing errors.
    """
    items = (wardrobe or {}).get("items") or []
    item_summary = (
        f"{new_item['title']} ({new_item['category']}; "
        f"colors: {', '.join(new_item['colors'])}; "
        f"tags: {', '.join(new_item['style_tags'])}) — {new_item['description']}"
    )

    if not items:
        prompt = (
            "You are a thrift-store stylist. Someone is considering buying this "
            f"secondhand piece:\n{item_summary}\n\n"
            "They haven't told you what's in their wardrobe. In 3-4 sentences, explain "
            "which kinds of bottoms, shoes, and layers pair well with it, which colors "
            f"work with its {', '.join(new_item['colors'])} tones, and what vibe or "
            "aesthetic it suits. Plain prose only: no lists, headings, or intro line."
        )
        advice = _chat(prompt, temperature=0.7, max_tokens=1024)
        if not advice:
            style = new_item["style_tags"][0] if new_item["style_tags"] else "classic"
            advice = f"pair the {new_item['title']} with simple {style} basics in neutral colors."
        return EMPTY_WARDROBE_PREFIX + advice

    # Names are quoted because some contain commas ("Baggy straight-leg jeans,
    # dark wash"), which the model otherwise treats as a separator and truncates.
    wardrobe_lines = "\n".join(
        f"- \"{item['name']}\" ({item['category']}; colors: {', '.join(item['colors'])}; "
        f"tags: {', '.join(item['style_tags'])}; notes: {item.get('notes') or 'none'})"
        for item in items
    )
    prompt = (
        "You are a thrift-store stylist. Someone is considering buying this "
        f"secondhand piece:\n{item_summary}\n\n"
        f"Their wardrobe:\n{wardrobe_lines}\n\n"
        "Suggest 1-2 complete outfits that combine the new piece with 2-4 pieces "
        "from their wardrobe.\n"
        "Rules:\n"
        "- Write each wardrobe piece name exactly as it appears inside the quotes "
        "above, including any text after a comma, but without the quote marks.\n"
        f"- Don't pair it with another {new_item['category']} piece.\n"
        "- Output only the outfit lines, one per line, in exactly this format:\n"
        f"Outfit 1: {new_item['title']} + <wardrobe piece> + <wardrobe piece> — "
        "<one sentence on why it works>"
    )
    suggestion = _chat(prompt, temperature=0.7, max_tokens=1024)
    # One outfit per line: the model sometimes adds blank lines or trailing spaces.
    suggestion = "\n".join(line.strip() for line in suggestion.splitlines() if line.strip())
    return suggestion or _fallback_outfit(new_item, items)


# ── Tool 3: create_fit_card ───────────────────────────────────────────────────

FIT_CARD_ERROR_PREFIX = "Couldn't create a fit card:"


def _short_title(title: str) -> str:
    """The part of a listing title before the em dash ("Graphic Tee — 2003 ..." -> "Graphic Tee")."""
    return title.split(" — ")[0]


def _template_caption(new_item: dict) -> str:
    """Deterministic caption used when the LLM returns no text (or, in agent.py, fails)."""
    return (
        f"thrifted this {_short_title(new_item['title']).lower()} off {new_item['platform']} "
        f"for ${new_item['price']:.0f} and it already has a spot in my rotation."
    )


def create_fit_card(outfit: str, new_item: dict) -> str:
    """
    Generate a short, shareable outfit caption for the thrifted find.

    Args:
        outfit:   The outfit suggestion string from suggest_outfit().
        new_item: The listing dict for the thrifted item.

    Returns:
        A 2–4 sentence string usable as an Instagram/TikTok caption.
        If outfit is empty or missing, or new_item lacks title/price/platform,
        returns an error message string starting with FIT_CARD_ERROR_PREFIX —
        does NOT raise. If the LLM returns no text, returns a template caption.

    The caption should:
    - Feel casual and authentic (like a real OOTD post, not a product description)
    - Mention the item name, price, and platform naturally (once each)
    - Capture the outfit vibe in specific terms
    - Sound different each time for different inputs (temperature 1.0)

    Raises:
        ValueError if GROQ_API_KEY is missing, or a groq.APIError subclass if the
        API call fails. The input guards run first, so they never need the API.
    """
    if outfit is None or not outfit.strip():
        return f"{FIT_CARD_ERROR_PREFIX} no outfit suggestion was provided for this item."

    new_item = new_item or {}
    missing = [key for key in ("title", "price", "platform") if new_item.get(key) in (None, "")]
    if missing:
        return f"{FIT_CARD_ERROR_PREFIX} the listing is missing {', '.join(missing)}."

    short_title = _short_title(new_item["title"]).lower()
    price = f"${new_item['price']:.0f}"
    condition = f" ({new_item['condition']} condition)" if new_item.get("condition") else ""
    prompt = (
        "Write a caption for an Instagram/TikTok outfit post about a thrifted piece.\n\n"
        f"The piece: {new_item['title']}{condition}, bought for {price} on {new_item['platform']}.\n"
        f"The outfit:\n{outfit.strip()}\n\n"
        "Rules:\n"
        "- 2-4 sentences, all lowercase, first person, casual: like a real OOTD post, "
        "not a product description.\n"
        f"- Mention the piece (a short name like \"{short_title}\" is fine), the price as "
        f"\"{price}\", and \"{new_item['platform']}\" exactly once each.\n"
        "- Name at least one specific piece from the outfit and describe the vibe in "
        "specific terms.\n"
        "- At most 2 emoji and at most 2 hashtags.\n"
        "- Output only the caption, with no surrounding quotes."
    )
    caption = _chat(prompt, temperature=1.0, max_tokens=512)
    return caption or _template_caption(new_item)


# ── Tool 4: parse_query ───────────────────────────────────────────────────────

_PARSE_SYSTEM_PROMPT = (
    "Extract the ONE clothing item the user wants to buy. Return JSON with keys "
    "description (short lowercase item phrase, no price or size words, exclude items "
    "they already own), size (string exactly as written, or null), max_price (number, "
    "or null)."
)

_PRICE_RE = re.compile(
    r"(?:under|below|less than|max|up to)\s*\$?\s*(\d+(?:\.\d+)?)|\$(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_SIZE_WORD_RE = re.compile(r"\bsize\s+((?:us\s+)?[a-z0-9./]+)", re.IGNORECASE)
# Case-sensitive so the "m" in "I'm" isn't read as a size.
_SIZE_BARE_RE = re.compile(r"\b(XXS|XS|S|M|L|XL|XXL)\b")
_FILLER_RE = re.compile(
    r"\b(i'?m|i am|looking for|i want|i need|find me|show me|a|an|some|in)\b",
    re.IGNORECASE,
)


def _regex_parse(query: str) -> dict:
    """Rule-based parse of the query's first sentence, used when the LLM parse fails."""
    # Split on sentence punctuation followed by whitespace, so "$29.99" stays whole.
    text = re.split(r"[.?!](?:\s|$)", query.strip(), maxsplit=1)[0]

    price_match = _PRICE_RE.search(text)
    max_price = float(price_match.group(1) or price_match.group(2)) if price_match else None
    text = _PRICE_RE.sub(" ", text)

    size_match = _SIZE_WORD_RE.search(text) or _SIZE_BARE_RE.search(text)
    size = size_match.group(1).upper() if size_match else None
    text = _SIZE_WORD_RE.sub(" ", text)
    text = _SIZE_BARE_RE.sub(" ", text)

    text = _FILLER_RE.sub(" ", text)
    text = re.sub(r"[^\w\s'-]", " ", text)
    description = re.sub(r"\s+", " ", text).strip().lower()
    return {"description": description, "size": size, "max_price": max_price}


def _validate_parse(data) -> dict | None:
    """Normalize the LLM's JSON into the parsed-query shape, or None if it's unusable."""
    if not isinstance(data, dict):
        return None

    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        return None

    size = data.get("size")
    if isinstance(size, bool) or not isinstance(size, (str, int, float, type(None))):
        return None
    size = str(size).strip() or None if size is not None else None

    max_price = data.get("max_price")
    if max_price is not None:
        if isinstance(max_price, bool) or not isinstance(max_price, (int, float)) or max_price <= 0:
            return None
        max_price = float(max_price)

    return {"description": description.strip().lower(), "size": size, "max_price": max_price}


def parse_query(query: str) -> tuple[dict, str]:
    """
    Turn a natural-language query into search_listings arguments.

    Args:
        query: The user's full message, e.g. "vintage graphic tee under $30, size M".

    Returns:
        (parsed, method) where parsed is
        {"description": str, "size": str | None, "max_price": float | None}
        and method is "llm" or "regex". description may be "" if no item could
        be found — the planning loop treats that as an error.

    Never raises for Groq failures, a missing API key, or bad JSON: any of those
    fall back to the regex parser.
    """
    try:
        reply = _chat(
            query,
            temperature=0,
            max_tokens=512,
            system=_PARSE_SYSTEM_PROMPT,
            json_mode=True,
        )
        parsed = _validate_parse(json.loads(reply))
    except (ValueError, APIError):  # ValueError covers bad JSON and a missing key
        parsed = None

    if parsed is not None:
        return parsed, "llm"
    return _regex_parse(query), "regex"
