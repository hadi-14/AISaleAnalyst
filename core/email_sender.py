import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from .config import SMTP_USER, SMTP_PASS, REPORT_EMAIL_TO

def send_report_email(report_path: str):
    """
    Sends the generated HTML report to the configured email addresses.
    """
    if not SMTP_USER or not SMTP_PASS or not REPORT_EMAIL_TO:
        print("\n  [Email] Missing SMTP configuration in .env. Skipping email.")
        return

    report_file = Path(report_path)
    if not report_file.exists():
        print(f"\n  [Email] Report file not found: {report_path}")
        return

    to_emails = [e.strip() for e in REPORT_EMAIL_TO.split(",") if e.strip()]
    if not to_emails:
        print("\n  [Email] No valid email addresses found in REPORT_EMAIL_TO.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"Estate Analyzer Report - {report_file.name}"
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(to_emails)
    
    msg.set_content(
        f"Hello,\n\nThe AI analysis has finished! Please find the attached report: {report_file.name}\n\n"
        "Best regards,\nAISaleAnalyst"
    )

    # Attach the HTML report
    with open(report_file, "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype="text",
            subtype="html",
            filename=report_file.name
        )

    print(f"\n  [Email] Sending report to: {', '.join(to_emails)} ...")
    try:
        # Connect to Gmail SMTP server
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        print("  [Email] Report successfully sent!")
    except Exception as e:
        print(f"  [Email] Failed to send email: {e}")
