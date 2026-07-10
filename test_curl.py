import google.auth
from google.auth.transport.requests import Request
import httpx
import os
import json

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "C:/Users/Misha/AppData/Local/hermes/application_default_credentials.json"

credentials, _ = google.auth.default()
credentials.refresh(Request())

project_id = "project-77ee3790-ced5-43a7-991"
location = "us-central1"
model = "gemini-1.5-pro-001"

url = f"https://{location}-aiplatform.googleapis.com/v1/projects/{project_id}/locations/{location}/publishers/google/models/{model}:generateContent"

headers = {
    "Authorization": f"Bearer {credentials.token}",
    "Content-Type": "application/json"
}
data = {
    "contents": [{"role": "user", "parts": [{"text": "Hello"}]}]
}
resp = httpx.post(url, headers=headers, json=data)
print(resp.status_code)
print(json.dumps(resp.json(), indent=2))
