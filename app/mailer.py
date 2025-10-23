import smtplib
from email.mime.text import MIMEText
from app.config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM

def send_email(to: str, subject: str, body: str):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to

    try:
        print(f"📧 Connecting to {SMTP_HOST}:{SMTP_PORT} as {SMTP_USER}")
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [to], msg.as_string())
        print(f"✅ Email sent successfully to {to}")
    except Exception as e:
        print(f"❌ Error sending email to {to}: {e}")
