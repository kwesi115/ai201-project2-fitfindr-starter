"""
tests/test_tools.py

Tests for the three FitFindr tools. Run from the repo root with:
    python -m pytest tests/

Failure-mode tests replace tools._chat with a stub, so they run offline and
never depend on what the LLM happens to say. The two tests marked `live` make
real Groq calls and are skipped when GROQ_API_KEY isn't set.
"""

import os

import pytest

import tools
from tools import (
    EMPTY_WARDROBE_PREFIX,
    FIT_CARD_ERROR_PREFIX,
    create_fit_card,
    search_listings,
    suggest_outfit,
)
from utils.data_loader import get_empty_wardrobe, get_example_wardrobe, load_listings

live = pytest.mark.skipif(
    not os.environ.get("GROQ_API_KEY"), reason="GROQ_API_KEY not set"
)

OUTFIT = (
    "Outfit 1: Graphic Tee — 2003 Tour Bootleg Style + Baggy straight-leg jeans, dark wash "
    "+ Chunky white sneakers — the boxy tee balances the relaxed denim."
)


@pytest.fixture
def graphic_tee():
    return next(listing for listing in load_listings() if listing["id"] == "lst_006")


@pytest.fixture
def fake_chat(monkeypatch):
    """Replace the LLM call with a stub that records prompts and returns `reply`."""
    calls = []

    def install(reply=""):
        def _fake(prompt, temperature, max_tokens):
            calls.append({"prompt": prompt, "temperature": temperature})
            return reply
        monkeypatch.setattr(tools, "_chat", _fake)
        return calls

    return install


# ── search_listings ───────────────────────────────────────────────────────────

def test_search_returns_results():
    results = search_listings("vintage graphic tee", size=None, max_price=50)
    assert isinstance(results, list)
    assert len(results) > 0


def test_search_empty_results():
    results = search_listings("designer ballgown", size="XXS", max_price=5)
    assert results == []


def test_search_price_filter():
    results = search_listings("jacket", size=None, max_price=10)
    assert all(item["price"] <= 10 for item in results)


def test_search_price_filter_is_inclusive_and_ranked():
    results = search_listings("vintage graphic tee", size=None, max_price=30.0)
    assert len(results) == 20
    assert all(item["price"] <= 30.0 for item in results)
    assert [item["id"] for item in results[:3]] == ["lst_006", "lst_033", "lst_015"]
    scores = [item["match_score"] for item in results]
    assert scores == sorted(scores, reverse=True)


def test_search_size_filter():
    results = search_listings("track jacket", size="M", max_price=None)
    assert results[0]["id"] == "lst_004"
    assert all(tools._size_matches("M", item["size"]) for item in results)


def test_search_size_filter_excludes_near_sizes():
    # The only boots near size 8 are US 8.5, which must not match "8".
    assert search_listings("black combat boots", size="8", max_price=None) == []


def test_search_ignores_color_only_matches():
    # Without the size filter, "black" alone would match shorts, tops, and jeans.
    results = search_listings("black combat boots")
    assert [item["id"] for item in results] == ["lst_028"]


def test_search_empty_description_returns_empty_list():
    assert search_listings("") == []


def test_search_does_not_mutate_dataset():
    search_listings("vintage graphic tee")
    assert all("match_score" not in listing for listing in load_listings())


# ── suggest_outfit ────────────────────────────────────────────────────────────

def test_suggest_outfit_empty_wardrobe_returns_general_advice(graphic_tee, fake_chat):
    fake_chat("pair it with dark denim and boots.")
    result = suggest_outfit(graphic_tee, get_empty_wardrobe())
    assert result == EMPTY_WARDROBE_PREFIX + "pair it with dark denim and boots."


@pytest.mark.parametrize("wardrobe", [{"items": []}, {}, None])
def test_suggest_outfit_handles_missing_or_empty_items(graphic_tee, fake_chat, wardrobe):
    fake_chat("some advice.")
    assert suggest_outfit(graphic_tee, wardrobe).startswith(EMPTY_WARDROBE_PREFIX)


def test_suggest_outfit_empty_wardrobe_and_empty_llm_reply_uses_fallback(graphic_tee, fake_chat):
    fake_chat("")
    result = suggest_outfit(graphic_tee, get_empty_wardrobe())
    assert result.startswith(EMPTY_WARDROBE_PREFIX)
    assert graphic_tee["title"] in result


def test_suggest_outfit_empty_llm_reply_uses_wardrobe_fallback(graphic_tee, fake_chat):
    fake_chat("")
    result = suggest_outfit(graphic_tee, get_example_wardrobe())
    assert result == (
        "Outfit 1: Graphic Tee — 2003 Tour Bootleg Style + Baggy straight-leg jeans, "
        "dark wash + Chunky white sneakers — an easy base to build on."
    )


def test_suggest_outfit_prompt_quotes_every_wardrobe_name(graphic_tee, fake_chat):
    calls = fake_chat(OUTFIT)
    wardrobe = get_example_wardrobe()
    suggest_outfit(graphic_tee, wardrobe)
    prompt = calls[0]["prompt"]
    assert all(f'"{item["name"]}"' in prompt for item in wardrobe["items"])
    assert calls[0]["temperature"] == 0.7


def test_suggest_outfit_propagates_missing_api_key(graphic_tee, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        suggest_outfit(graphic_tee, get_example_wardrobe())


# ── create_fit_card ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("outfit", ["", "   \n\t", None])
def test_fit_card_empty_outfit_returns_error_without_llm(graphic_tee, fake_chat, outfit):
    calls = fake_chat("should not be used")
    result = create_fit_card(outfit, graphic_tee)
    assert result == f"{FIT_CARD_ERROR_PREFIX} no outfit suggestion was provided for this item."
    assert calls == []


def test_fit_card_empty_outfit_works_without_api_key(graphic_tee, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert create_fit_card("", graphic_tee).startswith(FIT_CARD_ERROR_PREFIX)


def test_fit_card_missing_listing_fields_returns_error(graphic_tee, fake_chat):
    calls = fake_chat("should not be used")
    item = {key: value for key, value in graphic_tee.items() if key != "price"}
    assert create_fit_card(OUTFIT, item) == f"{FIT_CARD_ERROR_PREFIX} the listing is missing price."
    assert calls == []


def test_fit_card_empty_llm_reply_uses_template(graphic_tee, fake_chat):
    fake_chat("")
    assert create_fit_card(OUTFIT, graphic_tee) == (
        "thrifted this graphic tee off depop for $24 and it already has a spot in my rotation."
    )


def test_fit_card_prompt_includes_item_details(graphic_tee, fake_chat):
    calls = fake_chat("a caption.")
    create_fit_card(OUTFIT, graphic_tee)
    prompt = calls[0]["prompt"]
    assert "$24" in prompt and "depop" in prompt and OUTFIT in prompt
    assert calls[0]["temperature"] == 1.0


# ── live Groq calls ───────────────────────────────────────────────────────────

@live
def test_live_suggest_outfit_empty_wardrobe(graphic_tee):
    result = suggest_outfit(graphic_tee, get_empty_wardrobe())
    assert result.startswith(EMPTY_WARDROBE_PREFIX)
    assert len(result) > len(EMPTY_WARDROBE_PREFIX) + 20


@live
def test_live_fit_card_mentions_price_and_platform(graphic_tee):
    caption = create_fit_card(OUTFIT, graphic_tee)
    assert not caption.startswith(FIT_CARD_ERROR_PREFIX)
    assert "$24" in caption
    assert "depop" in caption.lower()
