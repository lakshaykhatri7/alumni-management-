# Alumni Data Management System

Python (Flask) + SQLite college project. Admin-only web app.

## Features
- Alumni CRUD (name, email, phone, batch, dept, company, designation, city, LinkedIn)
- Search/filter alumni by name/email/company, batch year, department
- Alumni profile page (donation history + event registrations)
- Events: create, list, register alumni to events
- Donations: record + total tracker
- Job board: post/delete jobs, link posted_by to alumni

## Setup
```
pip install -r requirements.txt
python3 app.py
```
Open http://localhost:5000 — DB (`alumni.db`) auto-creates on first run.

A `.env` file is included. Open it and paste your free Gemini key (optional — see AI Features below):
```
GEMINI_API_KEY=your_key_here
```
`app.py` loads it automatically via `python-dotenv`; no need to export env vars manually.

## Structure
```
app.py                 # routes + DB logic (sqlite3, no ORM)
templates/              # Jinja2 + Bootstrap 5 UI
alumni.db               # created at runtime
```

## DB Schema
alumni → events (many-to-many via event_registrations) → donations (1-alumni-to-many) → jobs (alumni optional FK)

## AI Features (all free)
1. **Semantic alumni search** (`ai_features.py`, sentence-transformers) — runs 100% locally,
   no API key, no cost. Toggle "AI" switch on Alumni page. Also powers job→alumni matching.
2. **AI chatbot + content generator** — uses Google Gemini free tier.
   - Get a free key: https://aistudio.google.com/apikey (no billing required for free tier)
   - Paste it into `.env` as `GEMINI_API_KEY=your_key`
   - Without a key, app runs fine; only chatbot/description-writer/thank-you-email buttons show a message.

## Possible extensions (for viva/demo)
- Login auth (Flask-Login) for admin
- Export alumni list to Excel/PDF
- Email notifications for events
- Charts (donations by year) with Chart.js
