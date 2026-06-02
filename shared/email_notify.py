"""Email notifications for SO creation with shipping label attachments."""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import datetime

from shared import config as cfg


def send_so_email(po_number, so_number, items, reg_result,
                  label_path=None, packing_path=None, bol_path=None):
    """
    Send email notification when a new SO is created.
    Attaches shipping label, packing slip, and (if available) BOL PDF.
    Non-fatal: logs and returns False on failure.
    """
    if not cfg.EMAIL_ENABLED or not cfg.EMAIL_APP_PASSWORD:
        print("  Email: disabled (EMAIL_ENABLED=false or no app password)")
        return False

    # Build item summary
    item_lines = ""
    for item in items:
        item_lines += (
            f"  - {item.get('wayfair_sku', '?')} -> {item.get('oracle_sku', '?')}  "
            f"qty: {item.get('ordered_qty', '?')}\n"
        )

    tracking = "N/A"
    carrier = "N/A"
    if reg_result:
        # Accept both snake_case (from BQ reg_log) and camelCase (fresh from Wayfair)
        tracking = (reg_result.get("tracking_number")
                    or reg_result.get("trackingNumber")
                    or "Not assigned")
        carrier  = (reg_result.get("carrier_code")
                    or reg_result.get("carrierCode")
                    or reg_result.get("carrier")
                    or "N/A")

    subject = f"New Wayfair SO: {so_number} -- PO {po_number}"

    body = f"""New Wayfair Sales Order has been created.

PO Number:       {po_number}
SO Number:       {so_number}
Created:         {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}

Items:
{item_lines}
Shipping:
  Tracking:      {tracking}
  Carrier:       {carrier}

Attachments:
  Shipping Label:  {'Attached' if label_path else 'Not available'}
  Packing Slip:    {'Attached' if packing_path else 'Not available'}
  Bill of Lading:  {'Attached' if bol_path else 'Not applicable (small parcel)'}

---
Automated notification from Wayfair Integration ({cfg.ENVIRONMENT})
"""

    try:
        msg = MIMEMultipart()
        msg["From"]    = cfg.EMAIL_ADDR
        msg["To"]      = cfg.EMAIL_TO
        if cfg.EMAIL_CC:
            msg["Cc"] = ", ".join(cfg.EMAIL_CC)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        # Attach shipping label PDF
        if label_path and os.path.exists(label_path):
            with open(label_path, "rb") as f:
                att = MIMEApplication(f.read(), _subtype="pdf")
                att.add_header("Content-Disposition", "attachment",
                               filename=os.path.basename(label_path))
                msg.attach(att)

        # Attach packing slip PDF
        if packing_path and os.path.exists(packing_path):
            with open(packing_path, "rb") as f:
                att = MIMEApplication(f.read(), _subtype="pdf")
                att.add_header("Content-Disposition", "attachment",
                               filename=os.path.basename(packing_path))
                msg.attach(att)

        # Attach Bill of Lading PDF (LTL only — usually absent for small parcel)
        if bol_path and os.path.exists(bol_path):
            with open(bol_path, "rb") as f:
                att = MIMEApplication(f.read(), _subtype="pdf")
                att.add_header("Content-Disposition", "attachment",
                               filename=os.path.basename(bol_path))
                msg.attach(att)

        all_recipients = [cfg.EMAIL_TO] + cfg.EMAIL_CC
        with smtplib.SMTP(cfg.SMTP_HOST, cfg.SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(cfg.EMAIL_ADDR, cfg.EMAIL_APP_PASSWORD)
            server.sendmail(cfg.EMAIL_ADDR, all_recipients, msg.as_string())

        print(f"  Email: sent to {cfg.EMAIL_TO} "
              f"{'cc=' + ','.join(cfg.EMAIL_CC) if cfg.EMAIL_CC else ''}")
        return True

    except Exception as e:
        print(f"  Email: FAILED (non-fatal) - {e}")
        return False
