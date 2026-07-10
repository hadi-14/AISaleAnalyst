import os
import logging
from dotenv import load_dotenv
from openai import OpenAI

# Load the API key from .env
load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    print("Error: OPENAI_API_KEY is not set in the .env file.")
    exit(1)

# Enable HTTP logging to see the exact request and rate limits
import httpx
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
httpx_logger.addHandler(ch)

print(f"Testing OpenAI API Key (starts with {api_key[:10]}...)...\n")

try:
    client = OpenAI(api_key=api_key)
    
    # Make a tiny, fast request to test authentication and limits
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Reply with 'API is working!'"}]
    )
    
    print("\n" + "="*40)
    print("SUCCESS! Response from OpenAI:")
    print(response.choices[0].message.content)
    print("="*40)
    
except Exception as e:
    print("\n" + "="*40)
    print(f"API CALL FAILED: {e}")
    print("="*40)
