import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from .config import SMTP_USER, SMTP_PASS, REPORT_EMAIL_TO, DEV_MODE

def send_report_email(report_path: str, url: str | None = None, items: list | None = None):
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
    subject_prefix = "[TEST DEV_MODE] " if DEV_MODE else ""
    msg["Subject"] = f"{subject_prefix}Estate Analyzer Report - {report_file.name}"
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(to_emails)
    
    body = ""
    if DEV_MODE:
        body += "⚠️ WARNING: This report was generated using DEV_MODE. The results use a cheaper AI model and the total items analyzed were capped. These results may not be accurate for production valuation.\n\n"
        
    body += "Hello,\n\nThe AI analysis has finished! Please find the attached report.\n\n"
    
    if url:
        body += f"Listing URL: {url}\n\n"
        
    if items:
        total_items = len(items)
        total_buy = sum(item.get("ai", {}).get("estate_buy_price", 0) for item in items if isinstance(item.get("ai", {}).get("estate_buy_price"), (int, float)))
        
        total_profit = 0
        for item in items:
            fin = item.get("financials")
            if fin and isinstance(fin.get("profit"), (int, float)):
                total_profit += fin["profit"]
                
        body += f"--- Scan Summary ---\n"
        body += f"Total Unique Items Analyzed: {total_items}\n"
        body += f"Total Expected Net Profit: ${total_profit:,.2f}\n"
        body += f"Recommended Max Estate Buy Budget: ${total_buy:,.2f}\n"
        body += f"--------------------\n\n"

    body += "Best regards,\nAISaleAnalyst"
    
    msg.set_content(body)

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
