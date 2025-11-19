"""
Required OAuth Scopes
Go to api.slack.com/apps → Your App → OAuth & Permissions → Scopes and add these Bot Token Scopes:

app_mentions:read    (Essential for mentions)
chat:write          (You probably have this already)
channels:read       (To read channel info)
groups:read         (For private channels)
im:read             (For DMs)
mpim:read           (For group DMs)

Enable Event Subscriptions
Go to Event Subscriptions → Subscribe to bot events and add:
app_mention         (Essential!)

Environment Variables:
SLACK_BOT_TOKEN     (Required): Bot token for Slack API access
SLACK_SIGNING_SECRET (Required): Signing secret for HTTP mode webhook verification
GOOGLE_API_KEY      (Required): Google API key for Gemini API access
DISCOUNT_CODE       (Optional): The secret discount code to guard (default: 4b0daf70118becc1)
DISCOUNT_CODES      (Optional): Comma-separated list of discount codes; if provided this overrides DISCOUNT_CODE

System Prompt:
The system prompt is defined in prompt.py and uses the DISCOUNT_CODE environment variable.

"""
import os
import re
import json
import logging
import asyncio
from pathlib import Path
from collections import defaultdict
from google import genai
from google.genai import types
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient
from dotenv import load_dotenv
from prompt import get_system_prompt

# Load environment variables from .env file
load_dotenv()

# Configure logging (console only)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
# Track attempts and dynamic easy-mode thresholds per channel (start at 15, then +10 after a real code is given)
CHANNEL_STATE = defaultdict(lambda: {"attempts": 0, "interval": 15, "next_threshold": 15})
# Track discount code inventory in memory (persisted to disk between restarts)
inventory_lock = asyncio.Lock()
STATE_PATH = Path(__file__).parent / "state" / "discount_codes.json"

# Get environment variables
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
SHOULD_REPLY_IN_CHANNEL = os.environ.get("SHOULD_REPLY_IN_CHANNEL", "true").lower() == "true"

# Check for required environment variables
if not SLACK_BOT_TOKEN:
    raise ValueError("SLACK_BOT_TOKEN environment variable is required")

SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
if not SLACK_SIGNING_SECRET:
    raise ValueError("SLACK_SIGNING_SECRET environment variable is required")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY environment variable is required for Gemini API")

DISCOUNT_CODE = os.environ.get("DISCOUNT_CODE", "")
DISCOUNT_CODES_ENV = os.environ.get("DISCOUNT_CODES")
DISCOUNT_CODE_PATTERN = re.compile(r"discountcode=([^?&#\s]+)", re.IGNORECASE)
DEPRECATED_CODES = {"4b0daf70118becc1"}

def extract_discount_code(raw: str) -> str:
    """Normalize a code value. Accepts full URLs, query fragments, or raw codes."""
    # Remove all whitespace to avoid truncation when env values contain spaces/newlines
    cleaned = re.sub(r"\s+", "", raw.strip())
    match = DISCOUNT_CODE_PATTERN.search(cleaned)
    if match:
        return match.group(1)
    if "=" in cleaned:
        return cleaned.split("=")[-1]
    return cleaned

def parse_discount_codes() -> list[str]:
    """Parse discount codes from DISCOUNT_CODES, DISCOUNT_CODE, and DISCOUNT_CODE_* env vars."""
    codes: list[str] = []

    # Comma-separated list
    if DISCOUNT_CODES_ENV:
        codes.extend(
            extract_discount_code(code)
            for code in DISCOUNT_CODES_ENV.split(",")
            if code.strip()
        )

    # Single fallback
    if DISCOUNT_CODE:
        codes.append(extract_discount_code(DISCOUNT_CODE))

    # Numbered env vars (DISCOUNT_CODE_1, DISCOUNT_CODE_2, ...)
    numbered_keys = sorted(
        [key for key in os.environ if key.startswith("DISCOUNT_CODE_")],
        key=lambda k: int(k.split("_")[-1]) if k.split("_")[-1].isdigit() else k,
    )
    for key in numbered_keys:
        value = os.environ.get(key)
        if value:
            codes.append(extract_discount_code(value))

    # Preserve order but remove duplicates
    seen = set()
    unique_codes = []
    for code in codes:
        if code and code not in DEPRECATED_CODES and code not in seen:
            seen.add(code)
            unique_codes.append(code)
    if not unique_codes:
        raise ValueError("No valid discount codes configured; please set DISCOUNT_CODES or DISCOUNT_CODE_*")
    return unique_codes

DISCOUNT_CODES = parse_discount_codes()

def ensure_state_dir():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

def load_code_state() -> dict:
    """Load discount code state from disk; if missing, seed from env codes."""
    ensure_state_dir()
    if STATE_PATH.exists():
        try:
            with STATE_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
                available = [
                    c for c in data.get("available_codes", [])
                    if c not in DEPRECATED_CODES and c in DISCOUNT_CODES
                ]
                used = [
                    c for c in data.get("used_codes", [])
                    if c not in DEPRECATED_CODES and c in DISCOUNT_CODES
                ]
                last_given = data.get("last_given_code")
                # If new codes are added via env, append them to available if not already tracked
                known_codes = set(available + used)
                for code in DISCOUNT_CODES:
                    if code not in known_codes:
                        available.append(code)
                return {
                    "available_codes": available,
                    "used_codes": used,
                    "last_given_code": last_given,
                }
        except Exception as error:
            logger.warning(f"Could not load code state, re-seeding from env: {error}")
    return {
        "available_codes": DISCOUNT_CODES.copy(),
        "used_codes": [],
        "last_given_code": None,
    }

def save_code_state(state: dict) -> None:
    ensure_state_dir()
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

code_state = load_code_state()

# Log startup configuration
logger.info("=== SLACK BOT STARTUP ===")
logger.info(f"Bot Token: {SLACK_BOT_TOKEN[:12]}..." if SLACK_BOT_TOKEN else "No Bot Token")
logger.info(f"Signing Secret: {SLACK_SIGNING_SECRET[:12]}..." if SLACK_SIGNING_SECRET else "No Signing Secret")
logger.info(f"Google API Key: {GOOGLE_API_KEY[:12]}..." if GOOGLE_API_KEY else "No Google API Key")
logger.info(f"Discount codes (total {len(DISCOUNT_CODES)}): {', '.join(DISCOUNT_CODES)}")
logger.info("==========================")

# Initialize Google Gemini client
client = genai.Client(api_key=GOOGLE_API_KEY)

app = AsyncApp(
    token=SLACK_BOT_TOKEN,
    signing_secret=SLACK_SIGNING_SECRET
)
async_client = AsyncWebClient(token=SLACK_BOT_TOKEN)

async def call_llm(prompt: str, system_prompt: str) -> str:
    """Call the Google Gemini API using native genai client"""
    try:
        logger.info(f"🤖 Calling Gemini API with prompt length: {len(prompt)} chars")
        
        # Run the synchronous genai call in a thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=1024
                    )
                )
            )
        )
        
        response_content = response.text
        logger.info(f"✅ Gemini API success - Response length: {len(response_content)} chars")
        return response_content
        
    except Exception as error:
        logger.error(f'❌ Error calling Gemini API: {error}')
        return 'Sorry, I could not reach the Gemini service.'

# Listen for mentions (when someone tags the bot)
@app.event("app_mention")
async def handle_mention(event, say, client):
    """Handle when the bot is mentioned with @botname"""
    
    # Extract event details
    message_ts = event.get('ts')
    channel_id = event.get('channel')
    user_id = event.get('user')
    original_text = event.get('text', '')
    
    # Get user information
    try:
        user_info = await async_client.users_info(user=user_id)
        username = user_info['user']['name']
        display_name = user_info['user'].get('profile', {}).get('display_name', username)
        real_name = user_info['user'].get('profile', {}).get('real_name', username)
    except Exception as e:
        username = user_id
        display_name = user_id
        real_name = user_id
        logger.warning(f"Could not fetch user info for {user_id}: {e}")
    
    # Log detailed mention information
    logger.info("=== BOT MENTION RECEIVED ===")
    logger.info(f"Message ID: {message_ts}")
    logger.info(f"Channel ID: {channel_id}")
    logger.info(f"User ID: {user_id}")
    logger.info(f"Username: @{username}")
    logger.info(f"Display Name: {display_name}")
    logger.info(f"Real Name: {real_name}")
    logger.info(f"Original Message: {original_text}")
    logger.info("=============================")
    
    try:
        # Remove the bot mention from the text
        # The mention format is usually <@U1234567890> so we need to clean it
        cleaned_text = re.sub(r'<@[^>]+>\s*', '', original_text).strip()
        
        logger.info(f"Cleaned Message: {cleaned_text}")
        
        if cleaned_text:
            # Count attempts per channel and decide if this is an easy round
            state = CHANNEL_STATE[channel_id]
            state["attempts"] += 1
            attempts = state["attempts"]
            interval = state["interval"]
            next_threshold = state["next_threshold"]
            is_easy_round = attempts >= next_threshold
            logger.info(
                f"Channel {channel_id} has {attempts} attempts. "
                f"Next easy threshold: {next_threshold} (interval {interval}). Easy round? {is_easy_round}"
            )

            # Quick inventory stats
            available_count = len(code_state["available_codes"])
            used_count = len(code_state["used_codes"])
            total_count = available_count + used_count

            # Simple heuristics for direct handling
            lower_text = cleaned_text.lower()
            is_count_query = (
                ("how many" in lower_text or "how much" in lower_text or "how long" in lower_text)
                and ("code" in lower_text or "ticket" in lower_text)
            )
            is_code_request = ("discount" in lower_text or "promo code" in lower_text or "free ticket" in lower_text)

            # Handle count queries directly to avoid LLM hallucinations
            if is_count_query:
                response = (
                    f"We have {available_count} free ticket discount code(s) left "
                    f"({used_count} already issued, {total_count} total)."
                    if available_count > 0
                    else f"All {total_count} free ticket codes have been used. No more left."
                )
                await say(f"<@{user_id}> {response}", thread_ts=message_ts)
                if SHOULD_REPLY_IN_CHANNEL:
                    await say(f"<@{user_id}> {response}")
                return

            issued_real_code = False

            # Issue a real code only during easy mode and only if user is clearly asking
            if is_easy_round and is_code_request:
                async with inventory_lock:
                    if code_state["available_codes"]:
                        issued_code = code_state["available_codes"].pop(0)
                        code_state["used_codes"].append(issued_code)
                        code_state["last_given_code"] = issued_code
                        save_code_state(code_state)
                        available_count = len(code_state["available_codes"])
                        used_count = len(code_state["used_codes"])
                        total_count = available_count + used_count
                    else:
                        issued_code = None

                if issued_code:
                    issued_real_code = True
                    response = (
                        "Easy mode unlocked! Here's a real free ticket link (one-time use): "
                        f"https://events.humanitix.com/keep-our-community-safe-mlai-hackathon?discountcode={issued_code} "
                        f"(Codes left after this: {available_count}/{total_count})."
                    )
                else:
                    response = "Easy mode is on, but we're out of discount codes. 🥲"

                await say(f"<@{user_id}> {response}", thread_ts=message_ts)
                if SHOULD_REPLY_IN_CHANNEL:
                    await say(f"<@{user_id}> {response}")
                return

            if is_easy_round:
                # After any easy round, schedule the next threshold; extend interval if a real code was given
                if issued_real_code:
                    state["interval"] += 10
                state["next_threshold"] = attempts + state["interval"]

            giveaway_code = (
                code_state["available_codes"][0] if is_easy_round and code_state["available_codes"] else None
            )
            system_prompt = get_system_prompt(
                discount_codes=DISCOUNT_CODES,
                available_count=available_count,
                used_count=used_count,
                is_easy_round=is_easy_round,
                giveaway_code=giveaway_code,
            )
            logger.info("Sending to Gemini...")
            response = await call_llm(cleaned_text, system_prompt=system_prompt)
            logger.info(f"Gemini Response: {response}")

            if SHOULD_REPLY_IN_CHANNEL:
                # Reply in main channel
                await say(f"<@{user_id}> {response}")
            # Reply in thread
            await say(f"<@{user_id}> {response}", thread_ts=message_ts)
        else:
            logger.info("Empty message, sending default greeting")
            if SHOULD_REPLY_IN_CHANNEL:
                # Reply in main channel
                await say(f"<@{user_id}> Hi! How can I help you?")
            # Reply in thread
            await say(f"<@{user_id}> Hi! How can I help you?", thread_ts=message_ts)
            
    except Exception as error:
        logger.error(f'Error processing mention: {error}')
        await say('Sorry, I encountered an error processing your message.')

# Start the app
if __name__ == "__main__":
    logger.info("⚡️ Bolt app is starting...")
    logger.info("⚡️ Initializing Slack Bot...")
    
    # Get port from environment variable or default to 3000
    port = int(os.environ.get("PORT", 3000))
    logger.info(f"🚀 Starting HTTP server on port {port}...")
    
    try:
        app.start(port=port)
    except KeyboardInterrupt:
        logger.info("👋 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Bot crashed: {e}") 
