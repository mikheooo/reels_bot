import vertexai
from vertexai.generative_models import GenerativeModel, Part
import os

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "C:/Users/Misha/AppData/Local/hermes/application_default_credentials.json"
vertexai.init(project="project-77ee3790-ced5-43a7-991", location="us-central1")
model = GenerativeModel("gemini-1.5-pro-001")
with open("dummy.mp4", "wb") as f:
    f.write(b"0" * 1024)
part = Part.from_data(data=open("dummy.mp4", "rb").read(), mime_type="video/mp4")
try:
    resp = model.generate_content([part, "What is this?"])
    print(resp.text)
except Exception as e:
    print("ERROR:", e)
