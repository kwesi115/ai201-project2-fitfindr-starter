"""
agent.py

The FitFindr planning loop. Orchestrates the three tools in response to a
natural language user query, passing state between them via a session dict.

Complete tools.py and test each tool in isolation before implementing this file.

Usage (once implemented):
    from agent import run_agent
    from utils.data_loader import get_example_wardrobe

    result = run_agent(
        query="vintage graphic tee under $30, size M",
        wardrobe=get_example_wardrobe(),
    )
    print(result["fit_card"])
    print(result["error"])   # None on success
"""

from groq import APIConnectionError, APIError, RateLimitError

from tools import (
    FIT_CARD_ERROR_PREFIX,
    _size_matches,
    _template_caption,
    create_fit_card,
    parse_query,
    search_listings,
    suggest_outfit,
)


EMPTY_QUERY_MESSAGE = (
    "Tell me what you're looking for — for example: \"vintage graphic tee under $30, size M\"."
)


# ── session state ─────────────────────────────────────────────────────────────

def _new_session(query: str, wardrobe: dict) -> dict:
    """
    Initialize and return a fresh session dict for one user interaction.

    The session dict is the single source of truth for everything that happens
    during a run — it stores the original query, parsed parameters, tool results,
    and any error that caused early termination.

    You may add fields to this dict as needed for your implementation.
    """
    return {
        "query": query,              # original user query
        "parsed": {},                # extracted description / size / max_price
        "parse_method": None,        # "llm" or "regex"
        "search_results": [],        # list of matching listing dicts
        "relaxed_results": [],       # search without size/price, only after a miss
        "selected_item": None,       # top result, passed into suggest_outfit
        "wardrobe": wardrobe,        # user's wardrobe dict
        "outfit_suggestion": None,   # string returned by suggest_outfit
        "fit_card": None,            # string returned by create_fit_card
        "warnings": [],              # degraded-but-continued events (not shown in UI)
        "tool_calls": [],            # tool names, in the order they were called
        "error": None,               # set if the interaction ended early
    }


# ── error messages ────────────────────────────────────────────────────────────

def _explain_no_results(session: dict) -> str:
    """
    Build the error message for an empty search (planning.md Step 4). If the user
    set a size or price, run one relaxed search to name the closest listing and
    the filter that excluded it; stores that search in session["relaxed_results"].
    """
    parsed = session["parsed"]
    description, size, max_price = parsed["description"], parsed["size"], parsed["max_price"]

    if size is None and max_price is None:
        return (
            f"No listings matched \"{description}\". Try a broader word for the item "
            "(e.g. \"dress\", \"blazer\", \"boots\") — we carry tops, bottoms, outerwear, "
            "shoes, and accessories."
        )

    session["tool_calls"].append("search_listings (relaxed)")
    relaxed = search_listings(description, None, None)
    session["relaxed_results"] = relaxed
    if not relaxed:
        return (
            f"No listings matched \"{description}\" at any size or price, so loosening your "
            "filters won't help. Try a broader word for the item "
            "(e.g. \"dress\", \"blazer\", \"boots\")."
        )

    best = relaxed[0]
    filter_text = (f" in size {size}" if size is not None else "") + (
        f" under ${max_price:.0f}" if max_price is not None else ""
    )
    message = (
        f"Nothing matched \"{description}\"{filter_text}. The closest listing without your "
        f"filters is {best['title']} — ${best['price']:.2f}, size {best['size']}, "
        f"on {best['platform']}."
    )
    if size is not None and not _size_matches(size, best["size"]):
        message += f" It's listed as size {best['size']}, not {size}."
    if max_price is not None and best["price"] > max_price:
        message += f" It's ${best['price'] - max_price:.2f} over your budget."
    return message + " Search again without those filters to see it."


def _found(item: dict) -> str:
    return f"Found {item['title']} (${item['price']:.2f} on {item['platform']})"


# ── planning loop ─────────────────────────────────────────────────────────────

def run_agent(query: str, wardrobe: dict) -> dict:
    """
    Main agent entry point. Runs the FitFindr planning loop for a single
    user interaction and returns the completed session dict.

    Args:
        query:    Natural language user request
                  (e.g., "vintage graphic tee under $30, size M")
        wardrobe: User's wardrobe dict — use get_example_wardrobe() or
                  get_empty_wardrobe() from utils/data_loader.py

    Returns:
        The session dict after the interaction completes. Check session["error"]
        first — if it is not None, the interaction ended early and the other
        output fields (outfit_suggestion, fit_card) will be None.

    The steps below follow the Planning Loop section of planning.md. Each tool's
    output is written to the session and the next tool reads its input from the
    session, so values are passed forward rather than re-derived.
    """
    # Step 1: Initialize the session. An empty query ends here, before any tool runs.
    session = _new_session(query, wardrobe)
    if query is None or not query.strip():
        session["error"] = EMPTY_QUERY_MESSAGE
        return session

    # Step 2: Parse the query into description / size / max_price. parse_query
    # asks the LLM for JSON and falls back to a regex parser (planning.md Tool 4).
    session["tool_calls"].append("parse_query")
    parsed, method = parse_query(query)
    session["parsed"] = parsed
    session["parse_method"] = method
    if method == "regex":
        session["warnings"].append("Query parsed with regex fallback (LLM parser unavailable).")
    if not parsed["description"]:
        session["error"] = (
            f"I couldn't tell what item you want from \"{query}\". Name the piece — for "
            "example \"graphic tee\", \"cargo pants\", or \"chelsea boots\" — and add a "
            "size or price if you like."
        )
        return session

    # Step 3: Search. With no results, explain why and return: suggest_outfit and
    # create_fit_card must never run on empty input.
    session["tool_calls"].append("search_listings")
    session["search_results"] = search_listings(
        session["parsed"]["description"],
        session["parsed"]["size"],
        session["parsed"]["max_price"],
    )
    if not session["search_results"]:
        session["error"] = _explain_no_results(session)
        return session

    # Step 4: Select the top result (search_listings already ranked it first).
    session["selected_item"] = session["search_results"][0]

    # Step 5: Suggest outfits for the selected item. An empty wardrobe is handled
    # inside suggest_outfit; only API failures end the run here.
    session["tool_calls"].append("suggest_outfit")
    try:
        session["outfit_suggestion"] = suggest_outfit(
            session["selected_item"], session["wardrobe"]
        )
    except ValueError as exc:
        if "GROQ_API_KEY" not in str(exc):
            raise
        session["error"] = (
            f"{_found(session['selected_item'])}, but outfit ideas need the Groq API and "
            "GROQ_API_KEY isn't set. Add GROQ_API_KEY=your_key to a .env file in the "
            "project root and restart the app."
        )
        return session
    except RateLimitError:
        session["error"] = (
            f"{_found(session['selected_item'])}, but Groq's rate limit was hit while "
            "generating outfit ideas. Wait about 30 seconds and search again."
        )
        return session
    except (APIConnectionError, APIError) as exc:
        session["error"] = (
            f"{_found(session['selected_item'])}, but I couldn't reach the outfit generator "
            f"({type(exc).__name__}). Check your internet connection and search again."
        )
        return session

    # Step 6: Create the fit card from the stored outfit and item. An API failure
    # degrades to a template caption instead of ending the run.
    session["tool_calls"].append("create_fit_card")
    try:
        fit_card = create_fit_card(session["outfit_suggestion"], session["selected_item"])
    except APIError as exc:
        fit_card = _template_caption(session["selected_item"])
        session["warnings"].append(f"Fit card used template caption ({type(exc).__name__}).")

    if fit_card.startswith(FIT_CARD_ERROR_PREFIX):
        session["error"] = fit_card
        session["outfit_suggestion"] = None
        return session
    session["fit_card"] = fit_card

    # Step 7: Return the completed session.
    return session


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from utils.data_loader import get_example_wardrobe, get_empty_wardrobe

    print("=== Happy path: graphic tee ===\n")
    session = run_agent(
        query="looking for a vintage graphic tee under $30",
        wardrobe=get_example_wardrobe(),
    )
    print(f"Tools called: {session['tool_calls']}")
    if session["error"]:
        print(f"Error: {session['error']}")
    else:
        print(f"Found: {session['selected_item']['title']}")
        print(f"\nOutfit: {session['outfit_suggestion']}")
        print(f"\nFit card: {session['fit_card']}")

    print("\n\n=== No-results path ===\n")
    session2 = run_agent(
        query="designer ballgown size XXS under $5",
        wardrobe=get_example_wardrobe(),
    )
    print(f"Tools called: {session2['tool_calls']}")
    print(f"Error message: {session2['error']}")
    print(f"Fit card: {session2['fit_card']}")
