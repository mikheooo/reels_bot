import asyncio
import os
import sys

import pytest

pytestmark = pytest.mark.asyncio

# Set working directory to reels_bot to allow relative imports
os.chdir(os.path.abspath("C:/Users/Misha/reels_bot"))
sys.path.insert(0, os.path.abspath("C:/Users/Misha/reels_bot"))

from app.worker.tasks import get_raw_transcript
from app.worker.factcheck import extract_claims, search_exa_for_claim, validate_claims

@pytest.mark.integration
async def test_bot():
    url = "https://www.instagram.com/reel/DboTpPcMpX8/"
    video_path = "C:/Users/Misha/reels_bot/test_vid.mp4"
    
    print(f"Downloading {url} to {video_path}...")
    from app.worker.tasks import download_video
    await download_video(url, video_path)
    
    print("Extracting transcript...")
    transcript = await get_raw_transcript(video_path)
    print(f"Transcript excerpt: {transcript[:200]}...")
    
    print("Extracting claims...")
    claims = await extract_claims(transcript)
    print(f"Extracted {len(claims)} claims.")
    
    print("Searching for claims...")
    search_data = {}
    for c in claims:
        if c.claim_type == "fact":
            results = await search_exa_for_claim(c)
            search_data[c.statement] = results
            print(f"  Found {len(results)} results for: {c.statement[:50]}...")
            
    print("Validating claims (running new prompt)...")
    analysis = await validate_claims(claims, search_data)
    
    print("=== ЗАДАЧА И КРИТЕРИИ ГОТОВНОСТИ ===")
    if analysis.task_description:
        print(analysis.task_description)
    else:
        print("No task description generated.")

if __name__ == "__main__":
    asyncio.run(test_bot())
