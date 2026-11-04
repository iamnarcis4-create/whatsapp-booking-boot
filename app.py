"""WhatsApp booking assistant: Twilio (WhatsApp) + Claude (tool use) + SQLite.

Run:  python app.py   (then point the Twilio WhatsApp webhook at  <public-url>/whatsapp)
"""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
from dotenv import load_dotenv
from flask import Flask, Response, request
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()

TZ = ZoneInfo("Europe/London")
MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
DB = os.getenv("DB_PATH", "bookings.db")
OWNER = os.getenv("OWNER_WHATSAPP", "")            # e.g. whatsapp:+447700900123
TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")  # sandbox number
PUBLIC_URL = os.getenv("PUBLIC_URL", "")           # needed only if VALIDATE_TWILIO=1

# ---- Business setup: edit this block for each new client -------------------
BUSINESS = {
    "name": "Fade & Co Barbers",
    "address": "14 High Street, opposite the post office. Free parking behind the shop.",
    "hours_text": "Monday to Saturday, 9:00 to 18:00. Closed on Sundays.",
    "open": 9 * 60, "close": 18 * 60, "step": 30,   # minutes since midnight
    "closed_weekdays": [6],                          # Monday=0 ... Sunday=6
    "services": {
        "Skin fade": {"price": 22, "minutes": 40},
        "Cut and beard trim": {"price": 30, "minutes": 50},
        "Beard trim": {"price": 12, "minutes": 20},
        "Kids cut": {"price": 14, "minutes": 25},
    },
}
# -----------------------------------------------------------------------------

app = Flask(__name__)
claude = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
twilio = (TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
          if os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN") else None)
HISTORY: dict[str, list] = {}   # phone -> recent text turns (in memory; fine for a demo)
LOCK = threading.Lock()


# ---------------------------- database ---------------------------------------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS bookings(
            id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, name TEXT, service TEXT,
            start TEXT, reminded INTEGER DEFAULT 0, cancelled INTEGER DEFAULT 0)""")


def hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def free_slots(day: str):
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
    if d.weekday() in BUSINESS["closed_weekdays"]:
        return []
    with db() as c:
        taken = {r["start"] for r in c.execute(
            "SELECT start FROM bookings WHERE cancelled=0 AND start LIKE ?", (day + "%",))}
    now = datetime.now(TZ)
    out = []
    for m in range(BUSINESS["open"], BUSINESS["close"], BUSINESS["step"]):
        if f"{day} {hm(m)}" in taken:
            continue
        if d.replace(hour=m // 60, minute=m % 60) <= now + timedelta(minutes=30):
            continue
        out.append(hm(m))
    return out


# ---------------------------- tools Claude can call --------------------------
TOOLS = [
    {"name": "get_free_slots",
     "description": "List free appointment start times for one date (YYYY-MM-DD). Always call this before offering times.",
     "input_schema": {"type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"]}},
    {"name": "book_appointment",
     "description": "Book a confirmed appointment. Only call after the customer has agreed service, date, time and given their name.",
     "input_schema": {"type": "object", "properties": {
         "date": {"type": "string", "description": "YYYY-MM-DD"},
         "time": {"type": "string", "description": "HH:MM, 24h, must be one of the free slots"},
         "service": {"type": "string", "enum": list(BUSINESS["services"])},
         "customer_name": {"type": "string"}},
         "required": ["date", "time", "service", "customer_name"]}},
    {"name": "list_my_bookings",
     "description": "List this customer's upcoming bookings (with ids).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "cancel_booking",
     "description": "Cancel one of this customer's bookings by id.",
     "input_schema": {"type": "object", "properties": {"booking_id": {"type": "integer"}}, "required": ["booking_id"]}},
]


def notify(to, body):
    if twilio and to:
        try:
            twilio.messages.create(from_=TWILIO_FROM, to=to, body=body)
        except Exception as e:  # never break the chat because a notification failed
            print("notify failed:", e)


def run_tool(phone, name, args):
    try:
        if name == "get_free_slots":
            return json.dumps({"date": args["date"], "free": free_slots(args["date"]) or "none (closed or fully booked)"})
        if name == "book_appointment":
            if args["time"] not in free_slots(args["date"]):
                return json.dumps({"error": "That time is not free. Call get_free_slots and offer another."})
            with LOCK, db() as c:
                c.execute("INSERT INTO bookings(phone,name,service,start) VALUES(?,?,?,?)",
                          (phone, args["customer_name"], args["service"], f'{args["date"]} {args["time"]}'))
            notify(OWNER, f'New booking: {args["customer_name"]}, {args["service"]}, {args["date"]} {args["time"]}')
            return json.dumps({"ok": True, "note": "Confirmed. A reminder goes out 2 hours before."})
        if name == "list_my_bookings":
            now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
            with db() as c:
                rows = [dict(r) for r in c.execute(
                    "SELECT id,name,service,start FROM bookings WHERE phone=? AND cancelled=0 AND start>=? ORDER BY start",
                    (phone, now))]
            return json.dumps(rows)
        if name == "cancel_booking":
            with LOCK, db() as c:
                cur = c.execute("UPDATE bookings SET cancelled=1 WHERE id=? AND phone=? AND cancelled=0",
                                (args["booking_id"], phone))
            if not cur.rowcount:
                return json.dumps({"error": "No such booking for this customer."})
            notify(OWNER, f'Booking {args["booking_id"]} cancelled by customer.')
            return json.dumps({"ok": True})
    except Exception as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"error": "unknown tool"})


# ---------------------------- the AI -----------------------------------------
def system_prompt():
    now = datetime.now(TZ)
    services = "\n".join(f"- {n}: £{v['price']}, about {v['minutes']} min" for n, v in BUSINESS["services"].items())
    return f"""You are the WhatsApp assistant for {BUSINESS['name']}. Today is {now:%A %d %B %Y}, {now:%H:%M} UK time.

Services:
{services}
Opening hours: {BUSINESS['hours_text']}
Address: {BUSINESS['address']}

Rules:
- Reply like a friendly human on WhatsApp: short, plain text, no markdown, at most 3 short sentences. Reply in the customer's language.
- Never invent availability. Use get_free_slots before offering times, and offer at most 4 times.
- To book you need service, date, time and the customer's name. Ask for what is missing, one thing at a time. Read the details back and get a clear yes before calling book_appointment.
- After booking, confirm service, date, time and price, and say a reminder will arrive 2 hours before.
- To cancel, call list_my_bookings first, then cancel_booking.
- Anything you cannot answer (discounts, complaints, special requests): say the owner will get back to them, do not guess."""


def ai_reply(phone, text):
    hist = HISTORY.setdefault(phone, [])
    hist.append({"role": "user", "content": text})
    del hist[:-16]
    while hist and hist[0]["role"] != "user":
        hist.pop(0)
    msgs = list(hist)
    answer = "Sorry, something went wrong on my side. Please try again in a moment."
    for _ in range(6):
        r = claude.messages.create(model=MODEL, max_tokens=500, system=system_prompt(), tools=TOOLS, messages=msgs)
        if r.stop_reason == "tool_use":
            msgs.append({"role": "assistant", "content": r.content})
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": run_tool(phone, b.name, b.input)}
                for b in r.content if b.type == "tool_use"]})
            continue
        answer = "".join(b.text for b in r.content if b.type == "text").strip() or answer
        break
    hist.append({"role": "assistant", "content": answer})
    return answer


# ---------------------------- WhatsApp webhook -------------------------------
@app.post("/whatsapp")
def whatsapp():
    if os.getenv("VALIDATE_TWILIO") == "1":
        ok = RequestValidator(os.getenv("TWILIO_AUTH_TOKEN", "")).validate(
            PUBLIC_URL + "/whatsapp", request.form, request.headers.get("X-Twilio-Signature", ""))
        if not ok:
            return Response("forbidden", status=403)
    phone, text = request.form.get("From", ""), request.form.get("Body", "").strip()
    resp = MessagingResponse()
    if text:
        try:
            resp.message(ai_reply(phone, text))
        except Exception as e:
            print("AI error:", e)
            resp.message("Sorry, I'm having a problem right now. The team will get back to you shortly.")
    return Response(str(resp), mimetype="application/xml")


@app.get("/")
def health():
    return "Booking assistant is running."


# ---------------------------- reminders --------------------------------------
def send_due_reminders():
    now = datetime.now(TZ)
    lo, hi = now.strftime("%Y-%m-%d %H:%M"), (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    with db() as c:
        due = [dict(r) for r in c.execute(
            "SELECT * FROM bookings WHERE cancelled=0 AND reminded=0 AND start>? AND start<=?", (lo, hi))]
        for b in due:
            notify(b["phone"], f'Reminder: your {b["service"]} at {BUSINESS["name"]} is today at {b["start"][11:]}. '
                               f'Reply here if you need to change it.')
            c.execute("UPDATE bookings SET reminded=1 WHERE id=?", (b["id"],))


def reminder_loop():
    while True:
        try:
            send_due_reminders()
        except Exception as e:
            print("reminder error:", e)
        time.sleep(60)


init_db()
# Runs under gunicorn (Render) with a single worker, so one reminder thread is enough.
threading.Thread(target=reminder_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
