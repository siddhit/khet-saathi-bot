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


# Validate the JSON output from Claude and parse it into a dict.
def validate_and_parse_response(text: str) -> dict | None:
    """
    Parse Claude's response as JSON.
    Strips markdown code blocks if Claude wraps the JSON anyway.
    Returns parsed dict if valid and contains required fields, else None.
    """
    # Strip markdown code blocks if present (```json ... ``` or ``` ... ```)
    cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip(), flags=re.MULTILINE)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    # Check minimum required fields
    if "diagnosis" not in parsed or "immediate_actions" not in parsed:
        return None

    return parsed

def format_for_whatsapp(advice: dict) -> str:
    """
    Convert structured JSON advice into WhatsApp-formatted text.
    WhatsApp supports *bold*, _italic_, and • bullets.
    """
    lines = []

    # Primary diagnosis
    primary = advice["diagnosis"]["primary_suspect"].replace("_", " ").title()
    lines.append(f"*Most likely:* {primary}")

    # Other suspects
    others = advice["diagnosis"].get("other_suspects", [])
    if others:
        others_text = ", ".join(o.replace("_", " ") for o in others)
        lines.append(f"_Also check:_ {others_text}")

    # Immediate actions
    lines.append("\n*Do this today:*")
    for action in advice["immediate_actions"]:
        lines.append(f"• {action}")

    # Treatments
    treatments = advice.get("treatments", [])
    if treatments:
        lines.append("\n*If needed:*")
        for t in treatments:
            condition = t["condition"].replace("_", " ").title()
            product = t.get("product", "")
            dosage = t.get("dosage", "")
            lines.append(f"• *{condition}:* {product} — {dosage}")
            if t.get("repeat"):
                lines.append(f"  Repeat: {t['repeat']}")

    # First follow-up question only
    questions = advice.get("follow_up_questions", [])
    if questions:
        lines.append(f"\n{questions[0]}")

    return "\n".join(lines)

# A helper function to send a text message via the WhatsApp Graph API.
def send_whatsapp_message(to: str, body: str) -> None:
    """Fire-and-forget: POST a text message to a recipient via the Graph API."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    response = requests.post(GRAPH_API_URL, json=payload, headers=headers, timeout=10)
    response.raise_for_status()

# A helper function that extracts the sender's phone number and message text.
def extract_text_message(body: dict) -> tuple[str, str] | None:
    """
    Pull (sender_phone, message_text) from the webhook payload.
    Returns None if this isn't a text message (image, audio, status update, etc.).
    """
    try:
        entry = body["entry"][0]
        change = entry["changes"][0]["value"]

        # Status updates (read receipts, delivered confirmations) arrive on the
        # same endpoint — they have no "messages" key, only "statuses".
        if "messages" not in change:
            return None

        message = change["messages"][0]
        if message["type"] != "text":
            return None

        sender = message["from"]
        text = message["text"]["body"]
        return sender, text

    except (KeyError, IndexError):
        return None

def detect_language(text: str) -> str:
    """Identify the primary script in the message using Unicode block counts."""
    gujarati = sum(1 for c in text if "઀" <= c <= "૿")
    devanagari = sum(1 for c in text if "ऀ" <= c <= "ॿ")
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    scores = {"Gujarati": gujarati, "Hindi": devanagari, "English": latin}
    return max(scores, key=scores.get)


# A class extending BaseHTTPRequestHandler to handle incoming HTTP requests from Meta's webhook.
class handler(BaseHTTPRequestHandler):
    """
    Vercel's Python runtime expects a class named `handler` that extends
    BaseHTTPRequestHandler. Each HTTP method maps to a do_METHOD function.
    """
    # Meta's webhook verification handshake
    def do_GET(self):
        """
        Webhook verification handshake.
        Meta calls this once when you register the webhook URL in the console.
        """
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        mode = params.get("hub.mode", [None])[0]
        token = params.get("hub.verify_token", [None])[0]
        challenge = params.get("hub.challenge", [None])[0]

        if mode == "subscribe" and token == VERIFY_TOKEN:
            # Echo the challenge back — this is how Meta knows you control this URL
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(challenge.encode())
        else:
            self.send_response(403)
            self.end_headers()
    
    # Incoming message handler and also the one calling Claude API to generate a reply.
    def do_POST(self):
        """
        Incoming message handler.

        Phase 0 pattern (synchronous):
        - Parse payload
        - Call WhatsApp API to send reply
        - Return 200

        Phase 1+ will change this to:
        - Parse payload, enqueue job
        - Return 200 immediately
        - Worker sends reply asynchronously
        (Required once AI processing time exceeds ~3–5 seconds)
        """
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)

        # Always return 200 first — if we return an error, Meta will retry
        # repeatedly. A 200 tells Meta "received, handled."
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

        # Parse and process after acknowledging
        # Note: in Vercel serverless, code after writing the response still runs
        # within the same invocation — this is fine for Phase 0.
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            return

        result = extract_text_message(body)
        if result is None:
            return  # Not a text message — ignore for now

        sender, text = result
        language = detect_language(text)
        # prompt = f'''
        # You are an expert agronomist specialising in Saurashtra crops — onion, cotton, and groundnut. You have advised farmers in the Mota Asrana area near Mahuva, Gujarat for 20 years. You give practical, actionable advice tailored to each farmer's situation.

        # You are advising farm supervisors at a farm in Mota Asrana, near Mahuva, Gujarat, India on plant condition or seasonal strategy.

        # Rules:
        # - Use simple, everyday words — no technical jargon.
        # - Answer only what was asked — do not mix plant-level and strategy-level advice.
        # - No greetings, sign-offs, or preambles.
        # - You MUST respond ONLY in {language}. Do not use any other language.
        # '''

        prompt = f'''
        You are an expert agronomist specialising in Saurashtra crops — onion, cotton, and groundnut. You have advised farmers in the Mota Asrana area near Mahuva, Gujarat for 20 years. You give practical, actionable advice tailored to each farmer's situation.

        You are advising farm supervisors at a farm in Mota Asrana, near Mahuva, Gujarat, India on plant condition or seasonal strategy.

        Rules:
        - Use simple, everyday words — no technical jargon.
        - Answer only what was asked — do not mix plant-level and strategy-level advice.
        - No greetings, sign-offs, or preambles.
        - Always respond with valid JSON only. No prose before or after. No markdown code blocks.
        - JSON keys and enum-style slug values (primary_suspect, other_suspects, condition) must always be in English.
        - All human-readable text values (immediate_actions, follow_up_questions, product, dosage, method, timing, repeat) must be written in {language}.

        Your response must follow this exact structure:

        Example input: "Onion leaves turning yellow from the tips. Started 3 days ago on one side of the field."

        Example output:
        {{
        "diagnosis": {{
            "primary_suspect": "overwatering",
            "other_suspects": ["nitrogen_deficiency", "fungal_disease"]
        }},
        "immediate_actions": [
            "Stop watering for 3-4 days",
            "Check soil moisture by digging 5cm down — should be moist not wet",
            "Look for soft rot or white spots near the soil level"
        ],
        "treatments": [
            {{
            "condition": "nitrogen_deficiency",
            "product": "urea",
            "dosage": "2kg per 1000L water",
            "method": "foliar spray",
            "timing": "evening",
            "repeat": "every 10 days, 2 applications"
            }},
            {{
            "condition": "fungal_disease",
            "product": "Mancozeb",
            "dosage": "2.5g per 1L water",
            "repeat": "after 10 days"
            }}
        ],
        "follow_up_questions": [
            "Is the whole field yellow or just the one side?",
            "How wet does the soil feel right now?"
        ],
        "severity": "medium"
        }}
        '''
        

        # Claude API call
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=prompt,
            messages=[{"role": "user", "content": text}]
        )
        # reply = response.content[0].text 
        # send_whatsapp_message(sender, reply)

        reply = response.content[0].text

        # Validate and parse JSON
        parsed = validate_and_parse_response(reply)
        if parsed is None:
            send_whatsapp_message(sender, "Sorry, something went wrong. Please try again.")
            return

        # Format for WhatsApp and send
        formatted = format_for_whatsapp(parsed)
        send_whatsapp_message(sender, formatted)

    def log_message(self, format, *args):
        pass  # Suppress BaseHTTPRequestHandler's default stderr logging
