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

System Prompt:
The system prompt is defined in prompt.py and uses the DISCOUNT_CODE environment variable.

"""
import os
import re
import logging
import asyncio
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

# Optional environment variables with defaults
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY environment variable is required for Gemini API")

DISCOUNT_CODE = os.environ.get("DISCOUNT_CODE", "4b0daf70118becc1")

# Generate system prompt using the discount code
SYSTEM_PROMPT = get_system_prompt(DISCOUNT_CODE)

# Log startup configuration
logger.info("=== SLACK BOT STARTUP ===")
logger.info(f"Bot Token: {SLACK_BOT_TOKEN[:12]}..." if SLACK_BOT_TOKEN else "No Bot Token")
logger.info(f"Signing Secret: {SLACK_SIGNING_SECRET[:12]}..." if SLACK_SIGNING_SECRET else "No Signing Secret")
logger.info(f"Google API Key: {GOOGLE_API_KEY[:12]}..." if GOOGLE_API_KEY else "No Google API Key")
logger.info(f"Discount Code: {DISCOUNT_CODE}")
logger.info("==========================")

# Initialize Google Gemini client
client = genai.Client(api_key=GOOGLE_API_KEY)

app = AsyncApp(
    token=SLACK_BOT_TOKEN,
    signing_secret=SLACK_SIGNING_SECRET
)
async_client = AsyncWebClient(token=SLACK_BOT_TOKEN)

async def call_llm(prompt: str) -> str:
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
                    system_instruction=SYSTEM_PROMPT,
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
            logger.info("Sending to Gemini...")
            response = await call_llm(cleaned_text)
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