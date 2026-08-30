"""
PondSense - Email Reply Handler
----------------------------------
Polls your Gmail inbox for new (unread) messages, asks Gemini to draft
a reply, and sends it back - all via IMAP + SMTP, using the SAME
GMAIL_ADDRESS / GMAIL_APP_PASSWORD already in .env. No public webhook,
no hosting, no callback URL needed - this is pure polling, so you can
just run it manually or on a schedule (cron / Task Scheduler / a
simple while-loop with sleep).

IMPORTANT PREREQUISITE:
  IMAP must be enabled on the Gmail account:
    Gmail -> Settings (gear icon) -> See all settings -> Forwarding
    and POP/IMAP -> Enable IMAP -> Save Changes.
  This is separate from the App Password you already set up for SMTP -
  same account, different toggle.

SAFETY NOTE ON WHAT GETS REPLIED TO:
  This currently replies to unread email matching SUBJECT_FILTER, not
  every unread message in the inbox - a real risk if the Gmail account
  is also used for anything else. If this is a personal/shared inbox
  rather than a dedicated PondSense address, that filter is doing real
  work - don't remove it casually.

Requires: GEMINI_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD in .env
pip install python-dotenv requests --break-system-packages
"""

import os
import re
import time
import imaplib
import smtplib
import email
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from dotenv import load_dotenv

from farmer_data import get_farmer_by_email, add_farmer, add_pond, get_ponds_for_farmer

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")

GEMINI_MODEL = "gemini-flash-latest"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

# Guardrail: only auto-reply to threads whose subject contains this.
# Set to "" to disable filtering (NOT recommended on a shared inbox).
SUBJECT_FILTER = "PondSense"

# Base URL the location-pin link is built from. Free-tier ngrok hands
# out a NEW random URL every time it's restarted, so hardcoding one in
# .env goes stale fast. Instead, get_location_base_url() below asks
# ngrok's own local API (always running at 127.0.0.1:4040 while ngrok
# is up) for whatever URL is currently live, every time a JOIN reply
# is built. LOCATION_BASE_URL in .env is kept as a manual override -
# useful if you ever get a fixed domain, or if ngrok's local API isn't
# reachable for some reason (falls back to it, or the placeholder).
LOCATION_BASE_URL = os.environ.get("LOCATION_BASE_URL")
NGROK_LOCAL_API = "http://127.0.0.1:4040/api/tunnels"


def get_location_base_url() -> str:
    if LOCATION_BASE_URL:
        return LOCATION_BASE_URL
    try:
        resp = requests.get(NGROK_LOCAL_API, timeout=2)
        resp.raise_for_status()
        tunnels = resp.json().get("tunnels", [])
        for t in tunnels:
            if t.get("proto") == "https":
                return t["public_url"]
        if tunnels:
            return tunnels[0]["public_url"]
    except requests.RequestException:
        pass
    print("[WARNING] Could not reach ngrok's local API - is ngrok running? "
          "Location link in this reply will be broken.")
    return "https://NGROK-NOT-RUNNING"

# JOIN catfish shallow concrete
# No pond_id here on purpose - PondSense assigns pond numbers
# automatically (Pond 1, Pond 2, ...) per farmer, so nobody has to
# invent a unique ID themselves. Same registration concept as the SMS
# path (sms_webhook.py), just without the farmer-typed pond_id field.
# No zip either as of this version - location is captured afterward
# via the location-pin link, not typed at JOIN time.
JOIN_RE = re.compile(
    r"^\s*JOIN\s+(catfish|hybrid_striped_bass)\s+(\S+)\s+(\S+)\s*$",
    re.IGNORECASE,
)


def _farmer_id_from_email(email_address: str) -> str:
    """
    Deterministic farmer_id from an email address, so re-sending JOIN
    from the same address updates the same farmer instead of creating
    a duplicate (add_farmer/add_pond both upsert). Mirrors the phone
    -> farmer_id scheme in sms_webhook.py, just keyed by email.
    """
    safe = re.sub(r"[^a-zA-Z0-9]", "_", email_address.strip().lower())
    return f"farmer_email_{safe}"


# Matches the line Gmail/most clients insert right above quoted history,
# e.g. "On Thu, Aug 28, 2026 at 7:29 PM sweegehneris@gmail.com wrote:"
# Everything from this line onward is the PREVIOUS message being quoted
# back, not new content from the sender - it must never be matched
# against JOIN_RE or the "join" keyword fallback, or a reply-in-thread
# will pick up JOIN, "join", or any other trigger word sitting in the
# quoted copy of PondSense's own prior message.
_QUOTE_HEADER_RE = re.compile(
    r"^\s*On .{0,120} wrote:\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Outlook/other clients sometimes use this separator instead.
_OUTLOOK_SEP_RE = re.compile(
    r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_quoted_reply(body: str) -> str:
    """
    Returns only the NEW text a sender actually typed in a reply,
    cutting off everything from the first quote marker onward.
    Handles three common shapes:
      1. Gmail-style: "On <date>, <name> wrote:" followed by the
         quoted message (often additionally '>'-prefixed).
      2. Lines that are already '>'-prefixed with no header line
         above them (some mobile mail clients do this).
      3. Outlook-style "----- Original Message -----" separators.
    If none of these appear, the body is returned unchanged - most
    fresh (non-reply) emails have nothing to strip.
    """
    cut_points = []

    m = _QUOTE_HEADER_RE.search(body)
    if m:
        cut_points.append(m.start())

    m = _OUTLOOK_SEP_RE.search(body)
    if m:
        cut_points.append(m.start())

    # First '>'-prefixed line, in case there's no header line above it.
    for line_match in re.finditer(r"^\s*>", body, re.MULTILINE):
        cut_points.append(line_match.start())
        break

    if cut_points:
        body = body[: min(cut_points)]

    return body.strip()


def _decode(value) -> str:
    """Decode an email header that may be MIME-encoded."""
    if value is None:
        return ""
    parts = decode_header(value)
    decoded = ""
    for text, charset in parts:
        if isinstance(text, bytes):
            decoded += text.decode(charset or "utf-8", errors="replace")
        else:
            decoded += text
    return decoded


def _extract_body(msg: email.message.Message) -> str:
    """Pull plain-text body out of a possibly-multipart email."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "")
            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
        return ""
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        return ""


def _build_pond_context(sender_email: str) -> str:
    """
    Looks up the sender in Supabase and returns a short text summary
    of their ponds Gemini can reference. Returns an explicit "no
    record found" note if there's no match, rather than silently
    omitting it - this keeps the prompt honest about what's actually
    known vs. not, rather than letting Gemini guess.
    """
    try:
        result = get_farmer_by_email(sender_email)
    except Exception as e:
        print(f"[FARMER LOOKUP FAILED] {e}")
        return "No farmer record could be looked up (lookup error)."

    if result is None:
        return "No farmer record found for this email address."

    farmer, ponds = result
    if not ponds:
        return f"Farmer record found ({farmer.farmer_id}), but no ponds on file."

    lines = [f"Farmer ID: {farmer.farmer_id}"]
    for p in ponds:
        tier = p.last_tier.upper() if p.last_tier else "no data yet"
        lines.append(
            f"  - {p.pond_id}: {p.species}, depth={p.depth_category}, "
            f"construction={p.construction_type}, last known tier={tier}"
        )
    return "\n".join(lines)


def generate_reply_text(original_subject: str, original_body: str, sender: str) -> str:
    """
    Asks Gemini to draft a short, helpful reply as PondSense support.
    Falls back to a plain hardcoded message if the API call fails -
    a reply should still go out even if Gemini is down, so the farmer
    isn't left hanging.
    """
    fallback = (
        "Thanks for reaching out. We've received your message and "
        "will follow up shortly. If this is urgent, please contact "
        "support directly.\n\n- PondSense"
    )

    if not GEMINI_API_KEY:
        return fallback

    pond_context = _build_pond_context(sender)

    prompt = (
        "You are replying on behalf of PondSense, an aquaculture "
        "pond heat-risk alert service, to a farmer's email reply. "
        "Write a short, warm, helpful reply (3-5 sentences max). "
        "Below is the farmer's actual pond record from our database - "
        "use it if relevant to their question. If it says no record "
        "was found, or the record doesn't answer their question, say "
        "you'll follow up with the details rather than guessing. "
        "Never invent tier, temperature, or pond data not shown below. "
        "Sign off as '- PondSense'.\n\n"
        f"Original subject: {original_subject}\n"
        f"Farmer's message:\n{original_body.strip()[:1500]}\n\n"
        f"Farmer's pond record:\n{pond_context}"
    )

    try:
        response = requests.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return text.strip()
    except Exception as e:
        print(f"[GEMINI FAILED, using fallback reply] {e}")
        return fallback


# Transient SMTP codes worth retrying - these mean "try again shortly",
# not "this will never work". 421/450/451/452 are Gmail's standard
# soft-throttle/busy codes (e.g. rate-limiting after a burst of sends).
RETRYABLE_SMTP_CODES = {421, 450, 451, 452}
SEND_RETRY_DELAYS = [5, 15, 30]  # seconds, one entry per retry attempt


def send_reply(to_email: str, subject: str, body: str, in_reply_to, references) -> bool:
    """
    Sends a reply with proper threading headers (In-Reply-To /
    References) so it shows up as a reply in the farmer's inbox
    instead of a disconnected new email.

    Retries with backoff on transient SMTP errors (busy/throttled
    server, dropped connection) - a single 421 from Gmail shouldn't
    permanently silence a farmer's registration reply. Retries do NOT
    cover permanent failures (bad address, auth failure) - those fail
    fast since retrying won't fix them.
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("[REPLY SEND SKIPPED] GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set.")
        return False

    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

    msg = MIMEMultipart()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = to_email
    msg["Subject"] = reply_subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.attach(MIMEText(body, "plain"))

    max_attempts = len(SEND_RETRY_DELAYS) + 1  # first try + retries

    for attempt in range(max_attempts):
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
                server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                server.sendmail(GMAIL_ADDRESS, to_email, msg.as_string())
            print(f"[REPLY SENT] {to_email} - {reply_subject}")
            return True

        except smtplib.SMTPResponseException as e:
            # Has a real SMTP status code - only retry the transient ones.
            if e.smtp_code in RETRYABLE_SMTP_CODES and attempt < len(SEND_RETRY_DELAYS):
                delay = SEND_RETRY_DELAYS[attempt]
                print(
                    f"[REPLY SEND RETRY {attempt + 1}/{len(SEND_RETRY_DELAYS)}] "
                    f"{to_email}: ({e.smtp_code}) {e.smtp_error} - retrying in {delay}s"
                )
                time.sleep(delay)
                continue
            print(f"[REPLY SEND FAILED] {to_email}: ({e.smtp_code}) {e.smtp_error}")
            return False

        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError) as e:
            # No status code, but still looks like a transient network/
            # connection blip rather than a permanent rejection - retry.
            if attempt < len(SEND_RETRY_DELAYS):
                delay = SEND_RETRY_DELAYS[attempt]
                print(
                    f"[REPLY SEND RETRY {attempt + 1}/{len(SEND_RETRY_DELAYS)}] "
                    f"{to_email}: {e} - retrying in {delay}s"
                )
                time.sleep(delay)
                continue
            print(f"[REPLY SEND FAILED] {to_email}: {e}")
            return False

        except Exception as e:
            # Anything else (bad recipient, auth failure, etc.) is
            # treated as permanent - retrying won't fix it.
            print(f"[REPLY SEND FAILED] {to_email}: {e}")
            return False

    return False


def check_and_reply_to_unread(dry_run: bool = True) -> int:
    """
    Connects to Gmail via IMAP, finds unread messages (filtered by
    SUBJECT_FILTER), drafts a Gemini reply for each, and either
    prints it (dry_run=True) or actually sends it (dry_run=False).
    Returns the number of messages processed.
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("[SKIPPED] GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env.")
        return 0

    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST)
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    except imaplib.IMAP4.error as e:
        print(f"[IMAP LOGIN FAILED] {e}")
        print("Check that IMAP is enabled in Gmail settings (see module docstring).")
        return 0

    imap.select("INBOX")

    search_criteria = "UNSEEN"
    if SUBJECT_FILTER:
        search_criteria = f'(UNSEEN SUBJECT "{SUBJECT_FILTER}")'

    status, message_ids = imap.search(None, search_criteria)
    if status != "OK":
        print("[IMAP SEARCH FAILED]")
        imap.logout()
        return 0

    ids = message_ids[0].split()
    print(f"Found {len(ids)} unread message(s) matching filter.")

    processed = 0
    for msg_id in ids:
        # BODY.PEEK[] = fetch the full message WITHOUT setting \Seen.
        # Plain "RFC822" (or "BODY[]") auto-marks the message read the
        # instant it's fetched, regardless of what happens afterward -
        # that was the real bug: a failed send still "consumed" the
        # message, so it never got picked up on the next poll cycle.
        # \Seen now gets set explicitly, below, only after a
        # successful send.
        status, msg_data = imap.fetch(msg_id, "(BODY.PEEK[])")
        if status != "OK":
            continue

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        sender = _decode(msg.get("From"))
        subject = _decode(msg.get("Subject"))
        message_id_header = msg.get("Message-ID")
        body = _extract_body(msg)

        # Extract just the email address out of "Name <email@x.com>"
        sender_email = sender
        if "<" in sender and ">" in sender:
            sender_email = sender.split("<")[1].split(">")[0].strip()

        print(f"\n--- Processing: {subject} (from {sender_email}) ---")

        # Strip quoted reply history FIRST - without this, a reply-in-
        # thread that quotes PondSense's own prior "To register, send
        # JOIN ..." instructions would have the word "join" sitting in
        # the quoted portion, and both matches below would be checked
        # against text the farmer never actually typed.
        new_text = _strip_quoted_reply(body)

        # JOIN is handled deterministically, same as sms_webhook.py -
        # no Gemini involved, straight to Supabase. This lets farmers
        # without SMS access self-register over email instead.
        join_match = JOIN_RE.match(new_text)
        if join_match:
            species, depth_category, construction_type = join_match.groups()
            farmer_id = _farmer_id_from_email(sender_email)
            try:
                add_farmer(
                    farmer_id, "", email=sender_email,
                    sms_opted_in=False, email_opted_in=True,
                )
                # Auto-assign the next pond number for this farmer -
                # deterministically reconstructable later from
                # farmer_id + pond number, so nothing farmer-facing
                # ever needs to remember an ugly internal ID.
                existing_ponds = get_ponds_for_farmer(farmer_id)
                pond_number = len(existing_ponds) + 1
                pond_id = f"{farmer_id}_pond{pond_number}"

                add_pond(
                    pond_id, farmer_id,
                    species.lower(), depth_category.lower(), construction_type.lower(),
                )
                # add_pond generates a fresh location_token internally -
                # fetch the pond back so the confirmation reply can link
                # straight to that pond's location-pin page.
                saved_pond = next(
                    p for p in get_ponds_for_farmer(farmer_id) if p.pond_id == pond_id
                )
                location_link = f"{get_location_base_url()}/location/{saved_pond.location_token}"

                reply_text = (
                    f"Welcome to PondSense! You're registered - this is your "
                    f"Pond {pond_number} ({species.lower()}).\n\n"
                    f"One more step: pin your pond's exact location so we can "
                    f"send accurate alerts. Open this link while standing "
                    f"outside at the pond:\n{location_link}\n\n"
                    f"You'll get alerts at this email address.\n\n"
                    f"- PondSense"
                )
                print(f"[JOIN] {farmer_id} -> {pond_id} ({species.lower()})")
            except Exception as e:
                print(f"[JOIN FAILED] {e}")
                reply_text = (
                    "Couldn't register that pond - check the format and try "
                    "again below.\n\n- PondSense"
                )
        elif re.search(r"\bjoin\b", new_text, re.IGNORECASE):
            # Catches both a malformed JOIN command AND plain natural-
            # language requests like "I'd like to join PondSense" -
            # either way, the farmer wants to register and needs the
            # exact format, not a generic AI reply that doesn't
            # actually register anything.
            reply_text = (
                "To register, send an email with just this line in the body:\n"
                "JOIN <species> <depth> <construction>\n\n"
                "Example: JOIN catfish shallow concrete\n\n"
                "Species must be catfish or hybrid_striped_bass. "
                "Depth: shallow, medium, or deep. Construction: earthen_unlined, "
                "earthen_lined, concrete, or above_ground.\n\n"
                "We'll assign your pond a number automatically, and send you "
                "a link afterward to pin your pond's exact location - you "
                "don't need to pick an ID or know your coordinates.\n\n"
                "- PondSense"
            )
        else:
            reply_text = generate_reply_text(subject, body, sender_email)

        if dry_run:
            print(f"[DRY RUN REPLY] Would send to {sender_email}:")
            print(reply_text)
            # Deliberately NOT marking \Seen in dry_run - it's a
            # preview, not a real send, so the message should still be
            # there to process for real later.
        else:
            sent = send_reply(
                sender_email,
                subject,
                reply_text,
                in_reply_to=message_id_header,
                references=message_id_header,
            )
            if sent:
                imap.store(msg_id, "+FLAGS", "\\Seen")
            else:
                # Leave it unread on purpose - same self-healing model
                # as the FortyGuard-style poller: a failed attempt just
                # gets picked up again on the next 60s cycle instead of
                # being silently dropped forever.
                print(f"[LEFT UNREAD] {sender_email} - will retry next poll cycle")

        processed += 1

    imap.logout()
    return processed


# ---------------------------------------------------------------------------
# Continuous polling - runs forever, checking every POLL_INTERVAL_SECONDS
# ---------------------------------------------------------------------------

POLL_INTERVAL_SECONDS = 60


def run_forever(dry_run: bool = True) -> None:
    """
    Checks for new PondSense-related emails every POLL_INTERVAL_SECONDS,
    forever, until you stop it (Ctrl+C). Keep this terminal window open -
    closing it stops the polling. For truly hands-off operation across
    reboots, Windows Task Scheduler is more robust than this loop.
    """
    print(f"Polling every {POLL_INTERVAL_SECONDS}s. dry_run={dry_run}. Press Ctrl+C to stop.\n")
    try:
        while True:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{timestamp}] Checking inbox...")
            try:
                count = check_and_reply_to_unread(dry_run=dry_run)
                if count == 0:
                    print("  -> nothing new.")
            except Exception as e:
                # Never let one failed check kill the whole loop - log
                # and keep polling, since a transient network blip
                # shouldn't stop replies for the rest of the day.
                print(f"  -> check failed: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopped.")


# ---------------------------------------------------------------------------
# Manual test - run this file directly to check for and (dry-run) reply
# to unread PondSense-related emails
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Testing email_reply_handler.py ...")
    print(f"GEMINI_API_KEY set: {bool(GEMINI_API_KEY)}")
    print(f"GMAIL_ADDRESS set: {bool(GMAIL_ADDRESS)}")
    print(f"GMAIL_APP_PASSWORD set: {bool(GMAIL_APP_PASSWORD)}")
    print(f"Subject filter: '{SUBJECT_FILTER}' (empty = no filter)\n")

    # Two modes:
    #   - Single check (default): run once, print/send, exit.
    #   - Continuous: set RUN_FOREVER = True below to poll every
    #     POLL_INTERVAL_SECONDS until you Ctrl+C.
    #
    # DRY_RUN stays True until you've confirmed drafted replies look
    # right - flip to False only once you trust the output.
    RUN_FOREVER = True
    DRY_RUN = False

    if RUN_FOREVER:
        run_forever(dry_run=DRY_RUN)
    else:
        count = check_and_reply_to_unread(dry_run=DRY_RUN)
        print(f"\nProcessed {count} message(s).")
