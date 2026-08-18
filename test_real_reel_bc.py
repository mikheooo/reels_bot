import asyncio
import json
import os
import sys

# Ensure reels_bot is in python path
sys.path.insert(0, os.path.abspath("C:/Users/Misha/reels_bot"))

from dotenv import load_dotenv
load_dotenv("C:/Users/Misha/reels_bot/.env")

from app.worker.tasks import get_raw_transcript
from app.worker.factcheck import extract_claims, search_exa_for_claim, validate_claims
from app.worker.business_check import run_business_check, format_business_check_markdown


async def run_real_test():
    video_path = "C:/Users/Misha/reels_bot/test_vid.mp4"
    if not os.path.exists(video_path):
        print(f"Error: Video file {video_path} not found!")
        return

    print("=== 1. ИСХОДНЫЙ ТРАНСКРИПТ / ТЕКСТ ВИДЕО ===")
    transcript_cache = "C:/Users/Misha/reels_bot/temp_transcript.txt"
    if os.path.exists(transcript_cache):
        with open(transcript_cache, "r", encoding="utf-8") as f:
            transcript = f.read()
        print("Loaded cached transcript from disk.")
    else:
        print("Uploading test_vid.mp4 to Gemini Flash for transcript extraction...")
        transcript = await get_raw_transcript(video_path)
        with open(transcript_cache, "w", encoding="utf-8") as f:
            f.write(transcript)

    print(f"\n--- Transcript ({len(transcript)} chars) ---")
    print(transcript)
    print("-------------------------------------------\n")

    print("=== 2. ИЗВЛЕЧЕННЫЕ CLAIMS ===")
    claims = await extract_claims(transcript)
    for i, c in enumerate(claims, 1):
        print(f"{i}. [{c.claim_type.upper()}] {c.statement}")

    print("\n=== 3. СУЩЕСТВУЮЩИЙ FACT CHECK ===")
    search_data = {}
    for c in claims:
        if c.claim_type == "fact":
            res = await search_exa_for_claim(c)
            search_data[c.statement] = res

    factcheck_analysis = await validate_claims(claims, search_data)
    print("\n--- FactCheck Analysis JSON ---")
    print(json.dumps(factcheck_analysis.model_dump(), indent=2, ensure_ascii=False))

    print("\n=== 4. НОВЫЙ BUSINESS CHECK ===")
    business_check_res = await run_business_check(
        transcript=transcript,
        claims=claims,
        factcheck_analysis=factcheck_analysis,
        metadata={"source_url": "https://www.instagram.com/reel/DboTpPcMpX8/", "video_path": video_path}
    )

    print("\n--- BusinessCheck Result JSON ---")
    print(json.dumps(business_check_res.model_dump(), indent=2, ensure_ascii=False))

    print("\n=== 5. ИТОГОВЫЙ COMBINED ANALYSIS ===")
    factcheck_analysis.business_check = business_check_res
    
    combined_dict = {
        "fact_check": {
            "claims": [c.model_dump() for c in factcheck_analysis.claims],
            "viable_idea": factcheck_analysis.viable_idea,
            "task_description": factcheck_analysis.task_description
        },
        "business_check": business_check_res.model_dump()
    }
    
    print("\n--- Combined JSON ---")
    print(json.dumps(combined_dict, indent=2, ensure_ascii=False))

    print("\n--- Formatted Combined Markdown ---")
    from app.worker.tasks import format_analysis_markdown
    full_markdown = format_analysis_markdown(
        factcheck_analysis,
        mechanics_text=f"📝 **Сырой Транскрипт (Механика):**\n{transcript[:300]}..."
    )
    print(full_markdown)


if __name__ == "__main__":
    asyncio.run(run_real_test())
