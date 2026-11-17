"""Standalone test: checks if GROQ_API_KEY works, independent of the Flask app.
Run with: python test_gemini_key.py
"""
import os
from dotenv import load_dotenv

load_dotenv()

key = os.environ.get("GROQ_API_KEY", "")
print(f"Key loaded: {key[:8]}...{key[-4:]} (length {len(key)})" if key else "No GROQ_API_KEY found in .env")

if not key:
    raise SystemExit(1)

from openai import OpenAI

client = OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1")

try:
    resp = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[{"role": "user", "content": "Say 'hello' in one word."}],
    )
    print("SUCCESS:", resp.choices[0].message.content.strip())
except Exception as e:
    print("FAILED:", type(e).__name__, e)
