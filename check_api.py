import google.auth
from google.auth.transport.requests import Request
import httpx
import os
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "C:/Users/Misha/AppData/Local/hermes/application_default_credentials.json"

credentials, _ = google.auth.default()
credentials.refresh(Request())

project_id = "project-77ee3790-ced5-43a7-991"

url = f"https://serviceusage.googleapis.com/v1/projects/{project_id}/services/aiplatform.googleapis.com"
headers = {"Authorization": f"Bearer {credentials.token}"}
resp = httpx.get(url, headers=headers)
print("Vertex AI API status:", resp.json())
