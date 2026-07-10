import google.auth
from google.auth.transport.requests import Request
import httpx
import os
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "C:/Users/Misha/AppData/Local/hermes/application_default_credentials.json"

credentials, _ = google.auth.default()
credentials.refresh(Request())

project_id = "project-77ee3790-ced5-43a7-991"
location = "us-central1"

url = f"https://{location}-aiplatform.googleapis.com/v1/projects/{project_id}/locations/{location}/publishers/google/models"
headers = {"Authorization": f"Bearer {credentials.token}"}
resp = httpx.get(url, headers=headers)
if resp.status_code == 200:
    models = resp.json().get('models', [])
    for m in models:
        name = m.get('name', '')
        if 'gemini' in name.lower():
            print(name)
else:
    print("Error:", resp.status_code, resp.text)
