import os
import re
import json
import requests
import anthropic
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

VERIFY_TOKEN = os.environ["WHATSAPP_VERIFY_TOKEN"]
ACCESS_TOKEN = os.environ["WHATSAPP_ACCESS_TOKEN"]
PHONE_NUMBER_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]

GRAPH_API_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"

client = anthropic.Anthropic()


def validate_and_parse_response(text: str) -> dict | None:
    """Parse Claude's JSON response; strips markdown fences if present."""
    cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip(), flags=re.MULTILINE)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    known_fields = {"diagnosis", "immediate_actions", "treatments", "follow_up_questions", "severity"}
    if not known_fields.intersection(parsed.keys()):
        return None
    return parsed


def format_for_whatsapp(advice: dict) -> str:
    """Convert structured JSON advice into WhatsApp-formatted text."""
    lines = []

    diagnosis = advice.get("diagnosis")
    if diagnosis:
        primary = diagnosis["primary_suspect"].replace("_", " ").title()
        lines.append(f"*Most likely:* {primary}")
        others = diagnosis.get("other_suspects", [])
        if others:
            lines.append(f"_Also check:_ {', '.join(o.replace('_', ' ') for o in others)}")

    immediate_actions = advice.get("immediate_actions", [])
    if immediate_actions:
        lines.append("\n*Do this today:*")
        for action in immediate_actions:
            lines.append(f"• {action}")

    treatments = advice.get("treatments", [])
    if treatments:
        lines.append("\n*If needed:*")
        for t in treatments:
            condition = t["condition"].replace("_", " ").title()
            lines.append(f"• *{condition}:* {t.get('product', '')} — {t.get('dosage', '')}")
            if t.get("repeat"):
                lines.append(f"  Repeat: {t['repeat']}")

    questions = advice.get("follow_up_questions", [])
    if questions:
        lines.append(f"\n{questions[0]}")

    return "\n".join(lines)


def send_whatsapp_message(to: str, body: str) -> None:
    """POST a text message to a recipient via the WhatsApp Graph API."""
    requests.post(
        GRAPH_API_URL,
        json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}},
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"},
        timeout=10,
    ).raise_for_status()


def extract_text_message(body: dict) -> tuple[str, str] | None:
    """Return (sender_phone, message_text) or None if not a text message."""
    try:
        change = body["entry"][0]["changes"][0]["value"]
        if "messages" not in change:
            return None
        message = change["messages"][0]
        if message["type"] != "text":
            return None
        return message["from"], message["text"]["body"]
    except (KeyError, IndexError):
        return None


def detect_language(text: str) -> str:
    """Identify the primary script in the message using Unicode block counts."""
    scores = {
        "Gujarati": sum(1 for c in text if "઀" <= c <= "૿"),
        "Hindi": sum(1 for c in text if "ऀ" <= c <= "ॿ"),
        "English": sum(1 for c in text if c.isascii() and c.isalpha()),
    }
    return max(scores, key=scores.get)


SYSTEM_PROMPT = """You are an expert agronomist specialising in Saurashtra crops — onion, cotton, and groundnut. You have advised farmers in the Mota Asrana area near Mahuva, Gujarat for 20 years. You give practical, actionable advice tailored to each farmer's situation.

You are advising farm supervisors at a farm in Mota Asrana, near Mahuva, Gujarat, India on plant condition or seasonal strategy.

Rules:
- Use simple, everyday words — no technical jargon.
- Answer only what was asked — do not mix plant-level and strategy-level advice.
- No greetings, sign-offs, or preambles.
- Always respond with valid JSON only. No prose before or after. No markdown code blocks.
- JSON keys and enum-style slug values (primary_suspect, other_suspects, condition) must always be in English.
- All human-readable text values (immediate_actions, follow_up_questions, product, dosage, method, timing, repeat) must be written in {language}.
- If the message is a greeting or not an agricultural question, respond with ONLY a follow_up_questions field in JSON asking what problem they need help with. No diagnosis, no actions.

Respond with this JSON structure:
{
  "diagnosis": {"primary_suspect": "slug", "other_suspects": ["slug"]},
  "immediate_actions": ["..."],
  "treatments": [{"condition": "slug", "product": "...", "dosage": "...", "method": "...", "timing": "...", "repeat": "..."}],
  "follow_up_questions": ["..."],
  "severity": "low|medium|high"
}"""


class handler(BaseHTTPRequestHandler):
    """Vercel Python runtime expects a class named `handler` extending BaseHTTPRequestHandler."""

    def do_GET(self):
        """Webhook verification handshake — Meta calls this once on registration."""
        params = parse_qs(urlparse(self.path).query)
        mode = params.get("hub.mode", [None])[0]
        token = params.get("hub.verify_token", [None])[0]
        challenge = params.get("hub.challenge", [None])[0]

        if mode == "subscribe" and token == VERIFY_TOKEN:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(challenge.encode())
        else:
            self.send_response(403)
            self.end_headers()

    def do_POST(self):
        """Receive a WhatsApp message, call Claude, reply — then return 200."""
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", 0)))

        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            self._ok()
            return

        result = extract_text_message(body)
        if result is None:
            self._ok()
            return  # Status update or non-text — ignore

        sender, text = result
        language = detect_language(text)

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=SYSTEM_PROMPT.format(language=language),
                messages=[{"role": "user", "content": text}],
            )
            reply = response.content[0].text
            parsed = validate_and_parse_response(reply)
            if parsed:
                send_whatsapp_message(sender, format_for_whatsapp(parsed))
            else:
                send_whatsapp_message(sender, "Sorry, something went wrong. Please try again.")
        except Exception:
            pass  # Never let an exception prevent the 200 to Meta

        self._ok()

    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def log_message(self, format, *args):
        pass  # Suppress default stderr logging
