from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file
import os
from datetime import datetime
from dotenv import load_dotenv
import ai_features as ai
import reports
import db
from db import get_db, init_db, IntegrityError
from email_alerts import configure_mail, mail_configured, send_bulk_email

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "alumni_secret_key_change_me")
configure_mail(app)


# ---------- DASHBOARD ----------
@app.route("/")
def dashboard():
    conn = get_db()
    stats = {
        "alumni": conn.execute("SELECT COUNT(*) c FROM alumni").fetchone()["c"],
        "events": conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"],
        "donations": conn.execute("SELECT COALESCE(SUM(amount),0) s FROM donations").fetchone()["s"],
        "jobs": conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"],
    }
    recent_alumni = conn.execute("SELECT * FROM alumni ORDER BY id DESC LIMIT 5").fetchall()
    upcoming_events = conn.execute("SELECT * FROM events ORDER BY event_date ASC LIMIT 5").fetchall()
    conn.close()
    return render_template("dashboard.html", stats=stats, recent_alumni=recent_alumni, upcoming_events=upcoming_events)


# ---------- ALUMNI CRUD ----------
@app.route("/alumni")
def alumni_list():
    q = request.args.get("q", "").strip()
    batch = request.args.get("batch", "").strip()
    dept = request.args.get("dept", "").strip()
    smart = request.args.get("smart", "")
    conn = get_db()

    if q and smart:
        # AI semantic search (free, local sentence-transformers) - ranks by meaning not exact text
        all_alumni = conn.execute("SELECT * FROM alumni").fetchall()
        alumni = ai.semantic_search_alumni(q, all_alumni)
        conn.close()
        return render_template("alumni_list.html", alumni=alumni, q=q, batch=batch, dept=dept, smart=smart)

    sql = "SELECT * FROM alumni WHERE 1=1"
    params = []
    if q:
        sql += " AND (name LIKE ? OR email LIKE ? OR current_company LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if batch:
        sql += " AND batch_year = ?"
        params.append(batch)
    if dept:
        sql += " AND department LIKE ?"
        params.append(f"%{dept}%")
    sql += " ORDER BY name"
    alumni = conn.execute(sql, params).fetchall()
    conn.close()
    return render_template("alumni_list.html", alumni=alumni, q=q, batch=batch, dept=dept, smart=smart)


@app.route("/alumni/add", methods=["GET", "POST"])
def alumni_add():
    if request.method == "POST":
        conn = get_db()
        try:
            conn.execute(
                """INSERT INTO alumni (name,email,phone,batch_year,department,current_company,designation,city,linkedin)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (request.form["name"], request.form["email"], request.form.get("phone"),
                 request.form.get("batch_year") or None, request.form.get("department"),
                 request.form.get("current_company"), request.form.get("designation"),
                 request.form.get("city"), request.form.get("linkedin")),
            )
            conn.commit()
            flash("Alumni added.", "success")
            return redirect(url_for("alumni_list"))
        except IntegrityError:
            flash("Email already exists.", "danger")
        finally:
            conn.close()
    return render_template("alumni_form.html", alumni=None)


@app.route("/alumni/<int:id>/edit", methods=["GET", "POST"])
def alumni_edit(id):
    conn = get_db()
    if request.method == "POST":
        conn.execute(
            """UPDATE alumni SET name=?,email=?,phone=?,batch_year=?,department=?,
               current_company=?,designation=?,city=?,linkedin=? WHERE id=?""",
            (request.form["name"], request.form["email"], request.form.get("phone"),
             request.form.get("batch_year") or None, request.form.get("department"),
             request.form.get("current_company"), request.form.get("designation"),
             request.form.get("city"), request.form.get("linkedin"), id),
        )
        conn.commit()
        conn.close()
        flash("Alumni updated.", "success")
        return redirect(url_for("alumni_list"))
    alum = conn.execute("SELECT * FROM alumni WHERE id=?", (id,)).fetchone()
    conn.close()
    return render_template("alumni_form.html", alumni=alum)


@app.route("/alumni/<int:id>/delete", methods=["POST"])
def alumni_delete(id):
    conn = get_db()
    conn.execute("DELETE FROM alumni WHERE id=?", (id,))
    conn.commit()
    conn.close()
    flash("Alumni deleted.", "info")
    return redirect(url_for("alumni_list"))


@app.route("/alumni/<int:id>")
def alumni_profile(id):
    conn = get_db()
    alum = conn.execute("SELECT * FROM alumni WHERE id=?", (id,)).fetchone()
    donations = conn.execute("SELECT * FROM donations WHERE alumni_id=? ORDER BY donation_date DESC", (id,)).fetchall()
    events = conn.execute("""SELECT e.* FROM events e JOIN event_registrations r ON e.id=r.event_id
                              WHERE r.alumni_id=? ORDER BY e.event_date""", (id,)).fetchall()
    conn.close()
    return render_template("alumni_profile.html", alum=alum, donations=donations, events=events)


# ---------- EVENTS ----------
@app.route("/events")
def events_list():
    conn = get_db()
    events = conn.execute("""SELECT e.*, (SELECT COUNT(*) FROM event_registrations r WHERE r.event_id=e.id) reg_count
                              FROM events e ORDER BY event_date DESC""").fetchall()
    conn.close()
    return render_template("events_list.html", events=events)


@app.route("/events/add", methods=["GET", "POST"])
def events_add():
    if request.method == "POST":
        conn = get_db()
        conn.execute("INSERT INTO events (title,description,event_date,location) VALUES (?,?,?,?)",
                     (request.form["title"], request.form.get("description"),
                      request.form.get("event_date"), request.form.get("location")))
        conn.commit()
        if request.form.get("notify"):
            emails = [r["email"] for r in conn.execute("SELECT email FROM alumni").fetchall()]
            body = f"New event: {request.form['title']}\n{request.form.get('description','')}\nDate: {request.form.get('event_date','TBA')}\nLocation: {request.form.get('location','TBA')}"
            result = send_bulk_email(f"New Event: {request.form['title']}", body, emails)
            if result["skipped"]:
                flash("Event created. Email not sent (configure MAIL_USERNAME/MAIL_PASSWORD in .env).", "warning")
            else:
                flash(f"Event created. Notified {result['sent']} alumni by email.", "success")
        else:
            flash("Event created.", "success")
        conn.close()
        return redirect(url_for("events_list"))
    return render_template("event_form.html", mail_on=mail_configured())


@app.route("/events/<int:id>/delete", methods=["POST"])
def events_delete(id):
    conn = get_db()
    conn.execute("DELETE FROM events WHERE id=?", (id,))
    conn.commit()
    conn.close()
    flash("Event deleted.", "info")
    return redirect(url_for("events_list"))


@app.route("/events/<int:id>")
def events_detail(id):
    conn = get_db()
    event = conn.execute("SELECT * FROM events WHERE id=?", (id,)).fetchone()
    registered = conn.execute("""SELECT a.* FROM alumni a JOIN event_registrations r ON a.id=r.alumni_id
                                  WHERE r.event_id=?""", (id,)).fetchall()
    all_alumni = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall()
    conn.close()
    return render_template("event_detail.html", event=event, registered=registered, all_alumni=all_alumni)


@app.route("/events/<int:id>/register", methods=["POST"])
def events_register(id):
    alumni_id = request.form.get("alumni_id")
    conn = get_db()
    try:
        conn.execute("INSERT INTO event_registrations (event_id,alumni_id) VALUES (?,?)", (id, alumni_id))
        conn.commit()
        flash("Alumni registered for event.", "success")
    except IntegrityError:
        flash("Already registered.", "warning")
    conn.close()
    return redirect(url_for("events_detail", id=id))


# ---------- DONATIONS ----------
@app.route("/donations")
def donations_list():
    conn = get_db()
    donations = conn.execute("""SELECT d.*, a.name alumni_name FROM donations d
                                 JOIN alumni a ON a.id=d.alumni_id ORDER BY donation_date DESC""").fetchall()
    total = conn.execute("SELECT COALESCE(SUM(amount),0) s FROM donations").fetchone()["s"]
    all_alumni = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall()
    conn.close()
    return render_template("donations_list.html", donations=donations, total=total, all_alumni=all_alumni)


@app.route("/donations/add", methods=["POST"])
def donations_add():
    conn = get_db()
    conn.execute("INSERT INTO donations (alumni_id,amount,purpose,donation_date) VALUES (?,?,?,?)",
                 (request.form["alumni_id"], request.form["amount"], request.form.get("purpose"),
                  request.form.get("donation_date") or datetime.now().strftime("%Y-%m-%d")))
    conn.commit()
    conn.close()
    flash("Donation recorded.", "success")
    return redirect(url_for("donations_list"))


@app.route("/donations/<int:id>/delete", methods=["POST"])
def donations_delete(id):
    conn = get_db()
    conn.execute("DELETE FROM donations WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("donations_list"))


# ---------- JOBS ----------
@app.route("/jobs")
def jobs_list():
    conn = get_db()
    jobs = conn.execute("""SELECT j.*, a.name posted_by_name FROM jobs j
                            LEFT JOIN alumni a ON a.id=j.posted_by_id ORDER BY posted_at DESC""").fetchall()
    all_alumni = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall()
    conn.close()
    return render_template("jobs_list.html", jobs=jobs, all_alumni=all_alumni, mail_on=mail_configured())


@app.route("/jobs/add", methods=["POST"])
def jobs_add():
    conn = get_db()
    conn.execute("INSERT INTO jobs (posted_by_id,title,company,location,description) VALUES (?,?,?,?,?)",
                 (request.form.get("posted_by_id") or None, request.form["title"],
                  request.form.get("company"), request.form.get("location"), request.form.get("description")))
    conn.commit()
    if request.form.get("notify"):
        emails = [r["email"] for r in conn.execute("SELECT email FROM alumni").fetchall()]
        body = f"New job posted: {request.form['title']} at {request.form.get('company','-')}\nLocation: {request.form.get('location','-')}\n{request.form.get('description','')}"
        result = send_bulk_email(f"New Job: {request.form['title']}", body, emails)
        if result["skipped"]:
            flash("Job posted. Email not sent (configure MAIL_USERNAME/MAIL_PASSWORD in .env).", "warning")
        else:
            flash(f"Job posted. Notified {result['sent']} alumni by email.", "success")
    else:
        flash("Job posted.", "success")
    conn.close()
    return redirect(url_for("jobs_list"))


@app.route("/jobs/<int:id>/delete", methods=["POST"])
def jobs_delete(id):
    conn = get_db()
    conn.execute("DELETE FROM jobs WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("jobs_list"))


# ---------- AI FEATURES ----------
@app.route("/ai/chat")
def ai_chat_page():
    return render_template("ai_chat.html", gemini_on=ai.gemini_available())


@app.route("/ai/chat/ask", methods=["POST"])
def ai_chat_ask():
    question = request.json.get("question", "")
    result = ai.ask_alumni_chatbot(question)
    return jsonify(result)


@app.route("/ai/generate/event", methods=["POST"])
def ai_generate_event():
    data = request.json
    result = ai.generate_event_description(
        data.get("title", ""), data.get("bullets", ""), data.get("location", ""), data.get("date", "")
    )
    return jsonify(result)


@app.route("/ai/generate/thankyou", methods=["POST"])
def ai_generate_thankyou():
    data = request.json
    result = ai.generate_donation_thankyou(data.get("name", ""), data.get("amount", ""), data.get("purpose", ""))
    return jsonify(result)


@app.route("/jobs/<int:id>/match")
def jobs_match(id):
    conn = get_db()
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone()
    all_alumni = conn.execute("SELECT * FROM alumni").fetchall()
    conn.close()
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job_text = f"{job['title']} {job['company']} {job['location']} {job['description']}"
    matches = ai.match_alumni_to_job(job_text, all_alumni)
    return jsonify({"matches": [{"name": a["name"], "email": a["email"], "company": a["current_company"]} for a in matches]})


# ---------- REPORTS & VISUALS ----------
@app.route("/reports")
def reports_page():
    conn = get_db()
    by_batch = conn.execute("SELECT batch_year, COUNT(*) c FROM alumni WHERE batch_year IS NOT NULL GROUP BY batch_year ORDER BY batch_year").fetchall()
    by_dept = conn.execute("SELECT department, COUNT(*) c FROM alumni WHERE department IS NOT NULL AND department!='' GROUP BY department").fetchall()
    by_year_donation = conn.execute("""SELECT substr(donation_date,1,4) yr, SUM(amount) s
                                        FROM donations GROUP BY yr ORDER BY yr""").fetchall()
    top_donors = conn.execute("""SELECT a.name, SUM(d.amount) total FROM donations d JOIN alumni a ON a.id=d.alumni_id
                                  GROUP BY d.alumni_id, a.name ORDER BY total DESC LIMIT 5""").fetchall()
    conn.close()
    return render_template("reports.html",
        batch_labels=[r["batch_year"] for r in by_batch], batch_data=[r["c"] for r in by_batch],
        dept_labels=[r["department"] for r in by_dept], dept_data=[r["c"] for r in by_dept],
        year_labels=[r["yr"] for r in by_year_donation], year_data=[r["s"] for r in by_year_donation],
        top_donors=top_donors)


@app.route("/reports/alumni/excel")
def export_alumni_excel():
    conn = get_db()
    rows = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall()
    conn.close()
    buf = reports.alumni_excel(rows)
    return send_file(buf, download_name="alumni_directory.xlsx", as_attachment=True)


@app.route("/reports/alumni/pdf")
def export_alumni_pdf():
    conn = get_db()
    rows = conn.execute("SELECT * FROM alumni ORDER BY name").fetchall()
    conn.close()
    buf = reports.alumni_pdf(rows)
    return send_file(buf, download_name="alumni_directory.pdf", as_attachment=True)


@app.route("/reports/donations/excel")
def export_donations_excel():
    conn = get_db()
    rows = conn.execute("""SELECT d.*, a.name alumni_name FROM donations d
                            JOIN alumni a ON a.id=d.alumni_id ORDER BY donation_date DESC""").fetchall()
    total = conn.execute("SELECT COALESCE(SUM(amount),0) s FROM donations").fetchone()["s"]
    conn.close()
    buf = reports.donations_excel(rows, total)
    return send_file(buf, download_name="donations_report.xlsx", as_attachment=True)


@app.route("/reports/donations/pdf")
def export_donations_pdf():
    conn = get_db()
    rows = conn.execute("""SELECT d.*, a.name alumni_name FROM donations d
                            JOIN alumni a ON a.id=d.alumni_id ORDER BY donation_date DESC""").fetchall()
    total = conn.execute("SELECT COALESCE(SUM(amount),0) s FROM donations").fetchone()["s"]
    conn.close()
    buf = reports.donations_pdf(rows, total)
    return send_file(buf, download_name="donations_report.pdf", as_attachment=True)


init_db()  # runs on both `python app.py` and gunicorn/production startup

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
