#!/usr/bin/env python3
import json
import base64
import requests

# Load config
with open('config.json') as f:
    config = json.load(f)

reddit = config['reddit']

print("=" * 50)
print("Reddit Auth Debug")
print("=" * 50)
print(f"Username: {reddit['username']}")
print(f"Client ID length: {len(reddit['client_id'])} chars (should be ~14)")
print(f"Client Secret length: {len(reddit['client_secret'])} chars (should be ~27)")
print(f"Client ID first 4: {reddit['client_id'][:4]}...")
print(f"Client Secret first 4: {reddit['client_secret'][:4]}...")

# Check for common issues
if len(reddit['client_id']) > 20:
    print("⚠️  Client ID seems too long - might be using secret as ID?")
if len(reddit['client_secret']) < 20:
    print("⚠️  Client Secret seems too short - might be using ID as secret?")

# Test manual auth
print("\nTesting manual authentication...")
auth = base64.b64encode(f"{reddit['client_id']}:{reddit['client_secret']}".encode()).decode()
headers = {
    "Authorization": f"Basic {auth}",
    "User-Agent": f"ModlogWikiPublisher/1.0 by /u/{reddit['username']}"
}
data = {
    "grant_type": "password",
    "username": reddit['username'],
    "password": reddit['password']
}

response = requests.post(
    "https://www.reddit.com/api/v1/access_token",
    headers=headers,
    data=data
)

print(f"\nResponse Status: {response.status_code}")
print(f"Response Headers: {dict(response.headers)}")
print(f"Response Body: {response.text}")

if response.status_code == 401:
    print("\n❌ 401 Error - Credential Issues:")
    print("1. Verify your app at https://www.reddit.com/prefs/apps")
    print("2. Client ID = the string under 'personal use script' (shorter)")
    print("3. Client Secret = the 'secret' field (longer)")
    print("4. Make sure the app type is 'script' not 'web app'")
    print("5. Username should be just 'Bakerboy448' not 'u/Bakerboy448'")