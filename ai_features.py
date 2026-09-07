"""
AI features for Alumni Management System.
- Semantic search & job matching: sentence-transformers, runs 100% locally, no API key, no cost.
- Chatbot (NL -> SQL) & content generation: Google Gemini free tier (needs a free API key
  from https://aistudio.google.com/apikey). If GEMINI_API_KEY is not set, these features
  are disabled gracefully and the rest of the app still works.
"""
import os
import json

# ---------------- Local semantic search (free, offline) ----------------
_model = None


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        # small, fast, free model - downloads once (~80MB) then cached locally
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def semantic_search_alumni(query, alumni_rows, top_k=10):
    """alumni_rows: list of sqlite3.Row. Returns rows ranked by semantic similarity to query."""
    if not alumni_rows:
        return []
    from sentence_transformers import util
    model = get_model()
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


# ---------------- Gemini (free tier) - chatbot + content generation ----------------
def gemini_available():
    return bool(os.environ.get("GEMINI_API_KEY"))


def _gemini_client():
    from google import genai
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


ALUMNI_SCHEMA = """
Tables:
alumni(id, name, email, phone, batch_year, department, current_company, designation, city, linkedin)
events(id, title, description, event_date, location)
event_registrations(id, event_id, alumni_id)
donations(id, alumni_id, amount, purpose, donation_date)
jobs(id, posted_by_id, title, company, location, description, posted_at)
"""


def ask_alumni_chatbot(question):
    """Natural language -> SQL -> answer, using free Gemini tier. Runs against PostgreSQL."""
    if not gemini_available():
        return {"error": "AI chatbot needs a free Gemini API key. Set GEMINI_API_KEY env var (get one free at https://aistudio.google.com/apikey)."}

    client = _gemini_client()
    prompt = f"""You are a SQL generator for a PostgreSQL alumni database.
Schema:
{ALUMNI_SCHEMA}
Rules: Return ONLY valid PostgreSQL SELECT SQL, no explanation, no markdown fences, no semicolon at the end unless needed.
Question: {question}
SQL:"""

    resp = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
    sql = resp.text.strip().strip("`").replace("sql\n", "").strip()

    if not sql.lower().startswith("select"):
        return {"error": "Could not generate a safe query for that question."}

    try:
        import db as db_module
        conn = db_module.get_db()
        rows = conn.execute(sql).fetchall()
        conn.close()
        results = [dict(r) for r in rows]
    except Exception as e:
        return {"error": f"Query failed: {e}", "sql": sql}

    # Ask Gemini to phrase a natural answer from the results
    summary_prompt = f"""Question: {question}
SQL used: {sql}
Results (JSON): {json.dumps(results)[:3000]}
Give a short, direct natural-language answer (1-3 sentences) based only on these results."""
    summary_resp = client.models.generate_content(model="gemini-2.0-flash", contents=summary_prompt)

    return {"answer": summary_resp.text.strip(), "sql": sql, "row_count": len(results)}


def generate_event_description(title, bullet_points, location, date):
    if not gemini_available():
        return {"error": "Needs free Gemini API key (GEMINI_API_KEY)."}
    client = _gemini_client()
    prompt = f"""Write a short, engaging alumni event announcement (max 80 words).
Title: {title}
Key points: {bullet_points}
Location: {location}
Date: {date}
Return only the announcement text, no preamble."""
    resp = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
    return {"text": resp.text.strip()}


def generate_donation_thankyou(alumni_name, amount, purpose):
    if not gemini_available():
        return {"error": "Needs free Gemini API key (GEMINI_API_KEY)."}
    client = _gemini_client()
    prompt = f"""Write a warm, brief thank-you email (max 100 words) from a college alumni office to a donor.
Donor name: {alumni_name}
Amount: {amount}
Purpose: {purpose}
Return only the email body, no subject line."""
    resp = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
    return {"text": resp.text.strip()}
