from __future__ import annotations
import json
import re


def parse_stance_json(
    response: str,
    valid_stances: tuple[str, ...] = ("bullish", "bearish", "neutral"),
    extra_fields: tuple[str, ...] = (),
) -> dict:
    """
    Parses an LLM response expected to be a JSON object with a "stance" field
    (one of `valid_stances`) plus free-text fields (e.g. "reasoning", "risk").

    Same three-tier fallback pattern as agents/critic.py's `_parse_response()`:
    1. Direct JSON parse, after stripping markdown code fences and isolating
       the first `{...}` block in case the model wraps it in prose.
    2. Regex recovery of just the "stance" key from otherwise-malformed JSON
       (e.g. a missing closing quote elsewhere in the object).
    3. Last-resort keyword scan over the full response with the "stance" key
       itself stripped out first. This reintroduces the verdict/reasoning
       collision risk this JSON migration exists to avoid (a risk sentence
       mentioning "bearish pressure" could flip the read), but it only fires
       when the model ignores the JSON schema entirely, not on every call.

    Always returns a dict with "stance" (one of `valid_stances`, defaulting to
    the last entry — "neutral" for the default set — if nothing matches),
    "raw_text" holding the original response, and every key in `extra_fields`
    (missing ones default to None).
    """
    text = response.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE).strip()

    result: dict = {"stance": None, "raw_text": response}
    for f in extra_fields:
        result[f] = None

    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            stance = str(data.get("stance", "")).strip().lower()
            if stance in valid_stances:
                result["stance"] = stance
                for f in extra_fields:
                    if f in data and data[f] is not None:
                        result[f] = data[f]
                return result
        except (json.JSONDecodeError, ValueError):
            pass

    stance_match = re.search(r'"stance"\s*:\s*"(\w+)"', text, re.IGNORECASE)
    if stance_match and stance_match.group(1).lower() in valid_stances:
        result["stance"] = stance_match.group(1).lower()
        return result

    stripped = re.sub(r'"stance"\s*:\s*"?\w*"?', "", text, flags=re.IGNORECASE)
    lower = stripped.lower()
    for stance in valid_stances:
        if stance in lower:
            result["stance"] = stance
            return result

    result["stance"] = valid_stances[-1]
    return result


def parse_indicator_verdicts(response: str, indicators: tuple[str, ...]) -> dict[str, str]:
    """
    Parses a combined multi-indicator LLM response requesting one buy/sell/
    hold verdict per indicator as a single JSON object, e.g.
    `{"rsi": "BUY", "stoch_rsi": "HOLD", "bollinger": "SELL"}` — used by
    Session 47 item 11 to replace up to four separate single-indicator calls.

    Two-tier fallback, deliberately without a final full-text keyword scan:
    1. Direct JSON parse (fence-stripped, brace-isolated).
    2. Regex recovery of each individual `"indicator": "WORD"` pair from
       otherwise-malformed JSON.
    Unlike `parse_stance_json()`, indicators that are missing or unparseable
    default to "hold" rather than falling back to a blind keyword scan — with
    several indicators in one response, a stray BUY/SELL word in free text
    has no reliable way to be attributed to the right indicator (this is the
    same label-collision failure the benchmark observed on the free-text
    "one line per indicator" version this JSON schema replaces).
    """
    text = response.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE).strip()

    result = {ind: "hold" for ind in indicators}

    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            for ind in indicators:
                v = str(data.get(ind, "")).strip().upper()
                if v in ("BUY", "SELL", "HOLD"):
                    result[ind] = v.lower()
            return result
        except (json.JSONDecodeError, ValueError):
            pass

    for ind in indicators:
        m2 = re.search(rf'"{re.escape(ind)}"\s*:\s*"?(\w+)"?', text, re.IGNORECASE)
        if m2:
            v = m2.group(1).strip().upper()
            if v in ("BUY", "SELL", "HOLD"):
                result[ind] = v.lower()
    return result
