"""
AI features for Alumni Management System.
- Semantic search & job matching: sentence-transformers, runs 100% locally, no API key, no cost.
- Chatbot (NL -> SQL) & content generation: Groq free tier (needs a free API key
  from https://console.groq.com/keys). If GROQ_API_KEY is not set, these features
  are disabled gracefully and the rest of the app still works.
"""
import os
import json
import logging
import re
import concurrent.futures

log = logging.getLogger("app")

# ---------------- Local semantic search (free, offline) ----------------
_model = None
_model_unavailable = False


def get_model():
    """Loads the sentence-transformers model, downloading it on first use.
    Uses a timeout so a slow/missing internet connection can't hang requests forever -
    callers should catch the resulting exception and fall back to non-semantic scoring."""
    global _model, _model_unavailable
    if _model_unavailable:
        raise RuntimeError("Semantic model unavailable (previous load failed/timed out)")
    if _model is None:
        def _load():
            from sentence_transformers import SentenceTransformer
            # small, fast, free model - downloads once (~80MB) then cached locally
            return SentenceTransformer("all-MiniLM-L6-v2")

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_load)
            try:
                _model = future.result(timeout=15)
            except Exception:
                _model_unavailable = True
                raise
    return _model


def semantic_search_alumni(query, alumni_rows, top_k=10):
    """alumni_rows: list of sqlite3.Row. Returns rows ranked by semantic similarity to query."""
    if not alumni_rows:
        return []
    try:
        from sentence_transformers import util
        model = get_model()
    except Exception:
        log.warning("Semantic search model unavailable, returning unranked alumni")
        return alumni_rows[:top_k]
    texts = [
        f"{a['name']} {a['department'] or ''} {a['current_company'] or ''} "
        f"{a['designation'] or ''} {a['city'] or ''} batch {a['batch_year'] or ''}"
        for a in alumni_rows
    ]
    corpus_emb = model.encode(texts, convert_to_tensor=True)
    query_emb = model.encode(query, convert_to_tensor=True)
    scores = util.cos_sim(query_emb, corpus_emb)[0]
    ranked = sorted(zip(alumni_rows, scores.tolist()), key=lambda x: x[1], reverse=True)
    return [row for row, score in ranked[:top_k] if score > 0.15]


def match_alumni_to_job(job_text, alumni_rows, top_k=5):
    """Same embedding trick, used to suggest which alumni fit a job posting."""
    return semantic_search_alumni(job_text, alumni_rows, top_k=top_k)


# ---------------- Groq (free tier) - chatbot + content generation ----------------
# Groq offers a free, fast LLM API (OpenAI-compatible). Get a free key at https://console.groq.com/keys
GROQ_MODEL = "openai/gpt-oss-20b"


def gemini_available():
    return bool(os.environ.get("GROQ_API_KEY"))


def _llm_client():
    from openai import OpenAI
    return OpenAI(api_key=os.environ["GROQ_API_KEY"], base_url="https://api.groq.com/openai/v1")


def _llm_complete(prompt):
    client = _llm_client()
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content


def _llm_error_message(e):
    """Turn raw Groq/OpenAI API errors into a short, user-facing message."""
    log.exception("LLM API call failed")
    status = getattr(e, "status_code", None)
    if status == 401:
        return "AI assistant is temporarily unavailable: the Groq API key is invalid. Please generate a new key at https://console.groq.com/keys and update GROQ_API_KEY."
    if status == 429:
        return "AI assistant is rate-limited right now. Please try again in a moment."
    return "AI assistant is temporarily unavailable. Please try again later."


ALUMNI_SCHEMA = """
Tables:
alumni(id, name, email, phone, batch_year, department, current_company, designation, city, linkedin)
events(id, title, description, event_date, location)
event_registrations(id, event_id, alumni_id)
donations(id, alumni_id, amount, purpose, donation_date)
jobs(id, posted_by_id, title, company, location, description, posted_at)
"""


def ask_alumni_chatbot(question, history=None, current_user=None):
    """Natural language -> SQL -> answer, using free Groq tier. Runs against PostgreSQL."""
    if not gemini_available():
        return {"error": "AI chatbot needs a free Groq API key. Set GROQ_API_KEY env var (get one free at https://console.groq.com/keys)."}

    history = history or []
    conversation = "\n".join(
        f"{item.get('role', 'user').capitalize()}: {str(item.get('content', ''))[:1000]}"
        for item in history[-8:]
        if item.get("content")
    )
    current_user = current_user or {}
    if current_user.get("alumni_id"):
        identity_line = (
            f"The current logged-in user's alumni.id is {current_user.get('alumni_id')} and email is "
            f"'{current_user.get('email')}'. When the question says 'I', 'me', or 'my', substitute this literal "
            f"id/email directly into the SQL - never use a placeholder."
        )
    else:
        identity_line = "The current user has no linked alumni profile, so questions about 'me'/'my' cannot be answered from the alumni table."
    prompt = f"""You are a SQL generator for a PostgreSQL alumni database.
Schema:
{ALUMNI_SCHEMA}
Rules: Return ONLY valid, directly-executable PostgreSQL SELECT SQL - no explanation, no markdown fences, no semicolon at the end unless needed.
Never use parameter placeholders such as $1, %s, or ? - always inline literal values.
Only reference the tables/columns listed in the schema above; there is no users table.
{identity_line}
Previous conversation:
{conversation or '(none)'}
Use the previous conversation to resolve follow-up references, but answer the current question only.
Question: {question}
SQL:"""

    try:
        sql = _llm_complete(prompt)
    except Exception as e:
        return {"error": _llm_error_message(e)}
    sql = sql.strip().strip("`").replace("sql\n", "").strip()
    # a single trailing semicolon is harmless and common in LLM output - strip it before validating,
    # but keep blocking any semicolon elsewhere (that would indicate statement chaining)
    sql = sql.rstrip().rstrip(";").rstrip()

    if not sql.lower().startswith("select"):
        return {"error": "Could not generate a safe query for that question."}
    if re.search(r"\$\d+|%s|(?<!\w)\?(?!\w)", sql):
        return {"error": "That question needs information the AI couldn't resolve into a direct query. Try rephrasing, or use the Career Mentor page for mentor matches.", "sql": sql}
    # block statement chaining, comments, and any non-read-only keywords the LLM might slip in
    if ";" in sql or "--" in sql or "/*" in sql:
        return {"error": "That question produced an unsafe query. Try rephrasing.", "sql": sql}
    if re.search(r"\b(insert|update|delete|drop|alter|truncate|grant|revoke|copy|create|call|do)\b", sql, re.IGNORECASE):
        return {"error": "That question produced an unsafe query. Try rephrasing.", "sql": sql}

    try:
        import db as db_module
        conn = db_module.get_db()
        rows = conn.execute(sql).fetchall()
        conn.close()
        results = [dict(r) for r in rows]
    except Exception as e:
        return {"error": f"Query failed: {e}", "sql": sql}

    # Ask the LLM to phrase a natural answer from the results
    summary_prompt = f"""Question: {question}
SQL used: {sql}
Results (JSON): {json.dumps(results, default=str)[:3000]}
Give a short, direct natural-language answer (1-3 sentences) based only on these results."""
    try:
        answer = _llm_complete(summary_prompt)
    except Exception as e:
        return {"error": _llm_error_message(e)}

    return {"answer": answer.strip(), "sql": sql, "row_count": len(results)}


def generate_event_description(title, bullet_points, location, date):
    if not gemini_available():
        return {"error": "Needs free Groq API key (GROQ_API_KEY)."}
    prompt = f"""Write a short, engaging alumni event announcement (max 80 words).
Title: {title}
Key points: {bullet_points}
Location: {location}
Date: {date}
Return only the announcement text, no preamble."""
    try:
        text = _llm_complete(prompt)
    except Exception as e:
        return {"error": _llm_error_message(e)}
    return {"text": text.strip()}


def generate_donation_thankyou(alumni_name, amount, purpose):
    if not gemini_available():
        return {"error": "Needs free Groq API key (GROQ_API_KEY)."}
    prompt = f"""Write a warm, brief thank-you email (max 100 words) from a college alumni office to a donor.
Donor name: {alumni_name}
Amount: {amount}
Purpose: {purpose}
Return only the email body, no subject line."""
    try:
        text = _llm_complete(prompt)
    except Exception as e:
        return {"error": _llm_error_message(e)}
    return {"text": text.strip()}


def recommend_best_mentor(source_alumni, alumni_rows, career_goal="", top_k=3):
    """Suggest the best mentors using local semantic similarity plus profile overlap."""
    if not source_alumni or not alumni_rows:
        return []

    source_id = source_alumni.get("id")
    goal_text = (career_goal or "").strip()
    source_text = " ".join(
        filter(None, [
            goal_text,
            source_alumni.get("department") or "",
            source_alumni.get("designation") or "",
            source_alumni.get("current_company") or "",
            source_alumni.get("city") or "",
            "career guidance mentoring",
        ])
    )

    try:
        from sentence_transformers import util
        model = get_model()
        query_emb = model.encode(source_text, convert_to_tensor=True)
    except Exception:
        util = None
        model = None
        query_emb = None

    scored = []
    for alum in alumni_rows:
        if alum.get("id") == source_id:
            continue

        total = 0
        reasons = []
        if (source_alumni.get("department") or "").strip().lower() == (alum.get("department") or "").strip().lower():
            total += 5
            reasons.append("same department")
        if (source_alumni.get("current_company") or "").strip().lower() == (alum.get("current_company") or "").strip().lower():
            total += 4
            reasons.append("same company")
        if (source_alumni.get("city") or "").strip().lower() == (alum.get("city") or "").strip().lower():
            total += 3
            reasons.append("same city")
        if (source_alumni.get("batch_year") or "") == (alum.get("batch_year") or ""):
            total += 2
            reasons.append("same batch")

        text = " ".join(filter(None, [
            alum.get("name") or "",
            alum.get("department") or "",
            alum.get("designation") or "",
            alum.get("current_company") or "",
            alum.get("city") or "",
            "mentor career guidance",
        ]))

        semantic_score = 0.0
        if model is not None:
            try:
                candidate_emb = model.encode(text, convert_to_tensor=True)
                semantic_score = float(util.cos_sim(query_emb, candidate_emb)[0][0])
                total += round(semantic_score * 10)
            except Exception:
                semantic_score = 0.0

        if total <= 0:
            continue

        scored.append({
            "id": alum.get("id"),
            "name": alum.get("name"),
            "email": alum.get("email"),
            "department": alum.get("department"),
            "current_company": alum.get("current_company"),
            "designation": alum.get("designation"),
            "city": alum.get("city"),
            "batch_year": alum.get("batch_year"),
            "score": total,
            "reason": ", ".join(reasons[:3]) if reasons else "strong profile fit",
            "semantic_score": semantic_score,
        })

    scored.sort(key=lambda item: (-item["score"], item["name"]))
    return scored[:top_k]


def generate_serendipity_icebreaker(alum_a, alum_b):
    """Short intro message for two randomly-matched alumni (Serendipity Engine). Falls back to a
    template if the LLM is unavailable so the weekly matching job never breaks without an API key."""
    def fallback():
        bits = []
        if (alum_a.get("current_company") or "").strip() and alum_a.get("current_company") == alum_b.get("current_company"):
            bits.append(f"you both work at {alum_a['current_company']}")
        if (alum_a.get("department") or "").strip() == (alum_b.get("department") or "").strip() and alum_a.get("department"):
            bits.append(f"you're both from {alum_a['department']}")
        if (alum_a.get("city") or "").strip() == (alum_b.get("city") or "").strip() and alum_a.get("city"):
            bits.append(f"you're both based in {alum_a['city']}")
        reason = (" and ".join(bits)) if bits else "you're both part of the alumni network"
        return (
            f"Hey {alum_a['name']} & {alum_b['name']} - you've been randomly matched this week! "
            f"Looks like {reason}. Say hi and swap notes."
        )

    if not gemini_available():
        return fallback()
    prompt = f"""Write a short, friendly icebreaker message (max 50 words) introducing two college alumni
who have been randomly paired for a weekly "random coffee" networking match.
Person A: {alum_a.get('name')}, batch {alum_a.get('batch_year')}, {alum_a.get('department') or 'unknown dept'}, {alum_a.get('designation') or ''} at {alum_a.get('current_company') or 'unknown company'}, based in {alum_a.get('city') or 'unknown city'}.
Person B: {alum_b.get('name')}, batch {alum_b.get('batch_year')}, {alum_b.get('department') or 'unknown dept'}, {alum_b.get('designation') or ''} at {alum_b.get('current_company') or 'unknown company'}, based in {alum_b.get('city') or 'unknown city'}.
Mention any genuine overlap (company, department, city) if relevant, otherwise keep it general and warm.
Return only the message text, addressed to both by name, no preamble."""
    try:
        text = _llm_complete(prompt)
        return text.strip()
    except Exception:
        log.exception("Icebreaker generation failed, using fallback template")
        return fallback()
