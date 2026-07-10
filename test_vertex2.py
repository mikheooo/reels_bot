import vertexai
from vertexai.generative_models import GenerativeModel, Part
import os

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "C:/Users/Misha/AppData/Local/hermes/application_default_credentials.json"
regions = ["us-central1", "europe-west4", "us-east1", "us-west1", "global"]
models = ["gemini-1.5-pro-002", "gemini-1.5-pro-001"]

for region in regions:
    for m in models:
        try:
            vertexai.init(project="project-77ee3790-ced5-43a7-991", location=region)
            model = GenerativeModel(m)
            part = Part.from_data(data=b"0"*1024, mime_type="video/mp4")
            resp = model.generate_content([part, "Test"])
            print(f"SUCCESS: region={region}, model={m}")
            break
        except Exception as e:
            print(f"FAIL region={region}, model={m}: {str(e)[:100]}")
