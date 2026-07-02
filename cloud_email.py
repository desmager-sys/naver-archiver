"""
Gmail SMTP 이메일 발송 — smtplib + 앱 비밀번호 방식.
환경변수: GMAIL_FROM, GMAIL_TO, GMAIL_APP_PASSWORD
"""
import os, smtplib
from email.mime.text import MIMEText


def send_email(subject: str, body: str) -> bool:
    sender   = os.environ.get("GMAIL_FROM", "desmager@gmail.com")
    receiver = os.environ.get("GMAIL_TO",   "desmager@gmail.com")
    app_pw   = os.environ.get("GMAIL_APP_PASSWORD", "")

    if not app_pw:
        print("  [Email] GMAIL_APP_PASSWORD 없음 → 발송 건너뜀")
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = receiver

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(sender, app_pw)
            smtp.sendmail(sender, receiver, msg.as_string())
        print(f"  [Email] 발송 완료 → {receiver}")
        return True
    except Exception as e:
        print(f"  [Email] 발송 실패: {e}")
        return False
