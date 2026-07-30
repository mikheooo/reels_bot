import os
import logging
from google import genai

logging.basicConfig(level=logging.DEBUG)
logging.getLogger("httpx").setLevel(logging.DEBUG)

os.environ["GOOGLE_CLOUD_PROJECT"] = "project-77ee3790-ced5-43a7-991"
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"

client = genai.Client(vertexai=True)
try:
    client.models.generate_content(model='gemini-3.1-pro-preview', contents='Ping')
except Exception as e:
    print(e)
