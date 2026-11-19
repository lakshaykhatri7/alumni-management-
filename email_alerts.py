"""Email alerts. Uses Flask-Mail with any free SMTP (e.g. Gmail + app password).
Configure in .env: MAIL_USERNAME, MAIL_PASSWORD, MAIL_SERVER, MAIL_PORT.
If not configured, emails are skipped silently (rest of app keeps working)."""
import os
from flask_mail import Mail, Message

mail = Mail()


def configure_mail(app):
    app.config["MAIL_SERVER"] = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", 587))
    app.config["MAIL_USE_TLS"] = True
    app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME")
    app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD")
    app.config["MAIL_DEFAULT_SENDER"] = os.environ.get("MAIL_USERNAME")
    # Fail fast instead of hanging the whole worker if SMTP ports are blocked (e.g. Render)
    app.config["MAIL_TIMEOUT"] = int(os.environ.get("MAIL_TIMEOUT", 10))
    mail.init_app(app)


def mail_configured():
    return bool(os.environ.get("MAIL_USERNAME") and os.environ.get("MAIL_PASSWORD"))


def send_bulk_email(subject, body, recipients):
    """recipients: list of email strings. Sends one-by-one (bcc-safe for free tier limits)."""
    if not mail_configured() or not recipients:
        return {"sent": 0, "skipped": True}
    sent = 0
    for email in recipients:
        try:
            msg = Message(subject=subject, recipients=[email], body=body)
            mail.send(msg)
            sent += 1
        except Exception as e:
            print(f"Email failed for {email}: {e}")
    return {"sent": sent, "skipped": False}


def send_single_email(subject, body, recipient):
    """Sends to exactly one recipient. Returns True on success, False if skipped/failed."""
    if not mail_configured() or not recipient:
        print(f"Email skipped for {recipient}: MAIL_USERNAME/MAIL_PASSWORD not configured")
        return False
    try:
        msg = Message(subject=subject, recipients=[recipient], body=body)
        mail.send(msg)
        return True
    except Exception as e:
        print(f"Email failed for {recipient}: {e}")
        return False
