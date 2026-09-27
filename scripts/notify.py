#!/usr/bin/env python3
"""
Email notifications for JET Index score changes.

Subscriber list: a Google Form (one "Email address" field) feeds a Google
Sheet, which is published to the web as a plain CSV. No API keys needed to
read it -- just an HTTP GET.

Sending: Resend (https://resend.com). Needs RESEND_API_KEY in the
environment; if it's missing, notifications are skipped (not an error --
lets the data-refresh half of the job keep working even before email is
wired up).
"""
import csv
import io
import os

import requests

from scoring import TIER_COLOR, TIER_TEXT, TIER_WORD, composite, tier_for_score

# --- Configuration -----------------------------------------------------
# Replace SUBSCRIBERS_CSV_URL once the Google Sheet is published to the web
# as CSV (File > Share > Publish to web > the Responses sheet > CSV).
SUBSCRIBERS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTxD9yd7fGGOWtrWg4JZbZubzFCIN7ZZlRdr8HBXdiNJOQ6KZYe_w30WD6WBeGAl1wqBI3W3ccxM580/pub?gid=1325128289&single=true&output=csv"

RESEND_API_URL = "https://api.resend.com/emails"
FROM_EMAIL = "JET Index <onboarding@resend.dev>"
SITE_URL = "https://jetindx.com"
# Deep-links straight to the "What's driving this reading" section, since the whole point of
# the email is to pull people back to the site for the actual explanation rather than trying
# to reproduce it inline.
DRIVING_URL = f"{SITE_URL}/#driving"


def get_subscribers():
    """Return a sorted list of unique subscriber emails, or [] on any problem."""
    if not SUBSCRIBERS_CSV_URL or SUBSCRIBERS_CSV_URL.startswith("REPLACE_"):
        print("[info] Subscriber CSV URL not configured yet; skipping notifications.")
        return []
    try:
        r = requests.get(SUBSCRIBERS_CSV_URL, timeout=30)
        r.raise_for_status()
        rows = list(csv.reader(io.StringIO(r.text)))
    except Exception as e:
        print(f"[warn] Could not fetch subscriber list: {e}")
        return []
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    email_col = next((i for i, h in enumerate(header) if "email" in h), None)
    if email_col is None:
        print(f"[warn] No 'email' column found in subscriber sheet header: {header}")
        return []
    emails = set()
    for row in rows[1:]:
        if len(row) > email_col:
            e = row[email_col].strip()
            if e and "@" in e:
                emails.add(e)
    return sorted(emails)


def _fmt(v, suffix=""):
    return f"{v}{suffix}"


def send_change_notifications(old, new):
    """old/new are (yc, pe, ecy) tuples. Emails every subscriber about the change."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        print("[info] RESEND_API_KEY not set; skipping notifications.")
        return

    emails = get_subscribers()
    if not emails:
        print("No subscribers to notify.")
        return

    old_yc, old_pe, old_ecy = old
    new_yc, new_pe, new_ecy = new

    old_score = composite(old_yc, old_pe, old_ecy)
    new_score = composite(new_yc, new_pe, new_ecy)
    old_tier = tier_for_score(old_score)
    new_tier = tier_for_score(new_score)

    def _chip_cell(tier, score, caption):
        """One colored tier chip + its caption, as a small standalone table -- table-based
        layout (not flex/div) so the color block survives Outlook's Word rendering engine,
        not just Gmail/Apple Mail."""
        return f"""
        <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 auto;">
          <tr><td style="width:64px; height:64px; min-width:64px; background-color:{TIER_COLOR[tier]}; color:{TIER_TEXT[tier]};
                          border-radius:14px; font-family:'JetBrains Mono',ui-monospace,Menlo,monospace; font-weight:800;
                          font-size:30px; text-align:center; vertical-align:middle;">{tier}</td></tr>
        </table>
        <div style="font-size:11.5px; color:#71757d; margin-top:8px; text-align:center;">{caption}</div>
        <div style="font-size:12.5px; color:#16181c; font-weight:700; text-align:center;">{score:.2f} / 7</div>
        """

    subject = f"JET Index: now {new_tier} tier ({TIER_WORD[new_tier]}), {new_score:.2f} / 7"

    body_html = f"""
    <div style="font-family:-apple-system,Helvetica,Arial,sans-serif; max-width:480px; margin:0 auto; color:#16181c;">
      <h2 style="margin-bottom:4px; font-size:19px;">The JET Index reading just changed</h2>
      <p style="font-size:13.5px; color:#71757d; margin-top:0;">One or more of the three inputs moved enough to update today's composite reading.</p>

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:20px 0;">
        <tr>
          <td width="42%" align="center" style="vertical-align:top;">{_chip_cell(old_tier, old_score, 'Before')}</td>
          <td width="16%" align="center" style="vertical-align:middle; font-size:22px; color:#71757d;">&rarr;</td>
          <td width="42%" align="center" style="vertical-align:top;">{_chip_cell(new_tier, new_score, 'Now')}</td>
        </tr>
      </table>

      <table style="width:100%; font-size:13.5px; border-collapse:collapse; margin:18px 0;">
        <tr style="border-bottom:1.5px solid #16181c;">
          <th align="left" style="padding:6px 4px;">Input</th>
          <th align="left" style="padding:6px 4px;">Before</th>
          <th align="left" style="padding:6px 4px;">Now</th>
        </tr>
        <tr style="border-bottom:1px dashed #e7e2d8;">
          <td style="padding:6px 4px;">2s10s Yield Curve</td>
          <td style="padding:6px 4px;">{_fmt(old_yc, '%')}</td>
          <td style="padding:6px 4px; font-weight:700;">{_fmt(new_yc, '%')}</td>
        </tr>
        <tr style="border-bottom:1px dashed #e7e2d8;">
          <td style="padding:6px 4px;">Shiller PE (CAPE)</td>
          <td style="padding:6px 4px;">{_fmt(old_pe)}</td>
          <td style="padding:6px 4px; font-weight:700;">{_fmt(new_pe)}</td>
        </tr>
        <tr>
          <td style="padding:6px 4px;">Excess CAPE Yield</td>
          <td style="padding:6px 4px;">{_fmt(old_ecy, '%')}</td>
          <td style="padding:6px 4px; font-weight:700;">{_fmt(new_ecy, '%')}</td>
        </tr>
      </table>

      <p style="font-size:13px; color:#16181c; margin-bottom:14px;">Curious what's actually pushing the reading to {new_tier} tier? The site breaks down each input's percentile rank and recent trend, generated live from today's numbers.</p>
      <p style="text-align:center; margin:0 0 6px;"><a href="{DRIVING_URL}" style="display:inline-block; background:#16181c; color:#fff; padding:11px 20px; border-radius:8px; text-decoration:none; font-weight:600; font-size:13.5px;">See what's driving this reading &rarr;</a></p>

      <p style="color:#71757d; font-size:11.5px; margin-top:28px; border-top:1px dashed #e7e2d8; padding-top:12px;">
        You're receiving this because you signed up for JET Index change alerts at {SITE_URL}.
        Reply to this email if you'd like to stop receiving them.
      </p>
    </div>
    """

    for email in emails:
        try:
            r = requests.post(
                RESEND_API_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"from": FROM_EMAIL, "to": [email], "subject": subject, "html": body_html},
                timeout=30,
            )
            if r.status_code >= 300:
                print(f"[warn] Failed to email {email}: {r.status_code} {r.text}")
            else:
                print(f"Notified {email}")
        except Exception as e:
            print(f"[warn] Error emailing {email}: {e}")
