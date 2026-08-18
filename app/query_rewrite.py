"""Heuristic natural-language -> keyword query rewriting for metasearch.

Real Google absorbs questions like "who is the ceo of TNB in 2017"; the
metasearch engines behind genxng rank keyword queries far better. This
rewriter only touches queries that look like questions, only removes
interrogative/function words, and never invents terms — "ceo TNB 2017"
retrieves the answer where the verbatim question retrieved noise.

Consumers that already do their own query reformulation (recommended for
RAG pipelines) can disable this with QUERY_REWRITE=false.
"""

QUESTION_STARTERS = {
    "who", "whos", "who's", "what", "whats", "what's", "when", "where",
    "which", "why", "how", "is", "are", "was", "were", "do", "does", "did",
    "can", "could", "will", "would", "should", "has", "have", "had",
}

STOPWORDS = QUESTION_STARTERS | {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "by", "with",
    "and", "or", "be", "been", "being", "am", "it", "its", "it's", "that",
    "this", "these", "those", "there", "their", "his", "her", "my", "your",
    "me", "i", "you", "we", "they", "them", "us", "please", "tell", "about",
}


def rewrite_query(query: str) -> str | None:
    """Return a keyword rewrite, or None when the query should pass verbatim.

    Only queries that look like questions (interrogative first word or a
    trailing '?') are rewritten; keyword queries are never touched.
    """
    stripped = query.strip()
    tokens = stripped.split()
    if len(tokens) < 3:
        return None

    looks_like_question = (
        tokens[0].lower() in QUESTION_STARTERS or stripped.endswith("?")
    )
    if not looks_like_question:
        return None

    kept = []
    for token in tokens:
        cleaned = token.strip("?.,!\"'")
        if cleaned and cleaned.lower() not in STOPWORDS:
            kept.append(cleaned)

    if len(kept) < 2:
        return None  # too little left to be a meaningful query
    rewritten = " ".join(kept)
    if rewritten.lower() == stripped.rstrip("?").lower():
        return None  # nothing actually changed
    return rewritten
