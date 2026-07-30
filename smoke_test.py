import asyncio
import json
from dotenv import load_dotenv

# Load real keys from .env
load_dotenv("C:/Users/Misha/reels_bot/.env")

from app.worker.factcheck import extract_claims, search_exa_for_claim, validate_claims, qa_audit
from app.worker.schemas import VideoAnalysis

transcript = """
Привет! Вчера Google выкатили новую модель Gemini 1.5 Pro. 
Главная фишка — огромное контекстное окно на 1 миллион токенов. Это значит, что туда можно закинуть целую книгу или длинное видео. 
Доступ к ней стоит 20 долларов в месяц в рамках подписки Google One AI Premium. 
Ещё в сети ходят слухи, что OpenAI полностью закрывает бесплатный доступ к ChatGPT со следующего месяца, 
но лично я считаю, что это полная чушь и они просто пытаются нагнать панику. 
Подписывайся, чтобы быть в курсе!
"""

async def run_smoke_test():
    print("=== SMOKE TEST STARTED ===\n")
    print(f"TRANSCRIPT:\n{transcript}\n")
    
    try:
        # STEP A
        print("1. Extracting Claims (Gemini 3.1 Pro)...")
        claims = await extract_claims(transcript)
        for c in claims:
            print(f"  - [{c.claim_type.upper()}] {c.statement}")
            
        # STEP B
        print("\n2. Searching Exa for Facts...")
        search_data = {}
        for c in claims:
            if c.claim_type == "fact":
                print(f"  -> Searching: {c.statement}")
                results = await search_exa_for_claim(c)
                search_data[c.statement] = results
                print(f"     Found {len(results)} sources.")
                for r in results:
                    print(f"       * {r.url} (Domain: {r.domain}, Type: {r.source_type})")
                await asyncio.sleep(1.5) # Exa rate limit buffer

        # STEP C
        print("\n3. Validating Claims (Gemini 3.1 Pro)...")
        analysis = await validate_claims(claims, search_data)
        for c in analysis.claims:
            print(f"  - [{c.status.upper()}] {c.statement}")
            if c.status in ["подтверждено", "опровергнуто"]:
                print(f"    Source: {c.source_url}")
                print(f"    Quote: {c.exact_quote}")
            elif c.status == "не проверено":
                print(f"    Reason: {c.unverified_reason}")

        # STEP E
        print("\n4. Running QA Audit (Gemini 2.5 Flash + Exa + Jina)...")
        qa_result = await qa_audit(analysis)
        
        print("\n=== SMOKE TEST RESULTS ===")
        print(f"QA Approved: {qa_result.approved}")
        if not qa_result.approved:
            print("QA Reasons:")
            for r in qa_result.reasons:
                print(f" - {r}")
                
    except Exception as e:
        print(f"\nERROR DURING SMOKE TEST: {e}")

if __name__ == "__main__":
    asyncio.run(run_smoke_test())
