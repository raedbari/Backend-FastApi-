import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app import config


def send_email(to_email: str, subject: str, body: str):
    """
    ترسل إيميل حقيقي عبر Gmail SMTP باستخدام إعدادات .env
    """

    # ✅ إنشاء الرسالة
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM
    msg["To"] = to_email

    # ✉️ نص الإيميل (HTML + نص عادي)
    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif;">
        <h2 style="color:#0078D7;">{subject}</h2>
        <p>{body}</p>
        <br/>
        <p>تحياتنا 👋<br><b>Smart DevOps Platform</b></p>
      </body>
    </html>
    """

    msg.attach(MIMEText(body, "plain"))
    msg.attach(MIMEText(html_content, "html"))

    try:
        # 🔐 الاتصال بخادم Gmail
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.ehlo()
            server.starttls()  # تفعيل التشفير TLS
            server.login(config.SMTP_USER, config.SMTP_PASS)

            # 🚀 إرسال الإيميل
            server.sendmail(config.SMTP_FROM, to_email, msg.as_string())

        print(f"✅ Email sent to {to_email}")

    except Exception as e:
        print(f"❌ Error sending email to {to_email}: {e}")
