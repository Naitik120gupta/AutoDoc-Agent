import httpx
import json

BASE_URL = "http://127.0.0.1:8000"

TEST_CASES = {
    "standard": "Draft a 1-page Project Proposal for migrating our local servers to AWS. Include an executive summary, timeline, and rough cost estimate.",
    
    "complex": "Write a business report for our client about our Q3 performance — it needs to be detailed but I don't have the final numbers yet, and the client also asked for it to cover both technical progress and budget in the same document.",
}


def run_case(name, request_text):
    print(f"\n{'=' * 70}\nTEST CASE: {name}\nREQUEST: {request_text}\n{'=' * 70}")
    resp = httpx.post(f"{BASE_URL}/agent", json={"request": request_text}, timeout=120)
    resp.raise_for_status()
    data = resp.json()

    print("\n-- AGENT-GENERATED PLAN --")
    for step in data["plan"]:
        print(f"  [{step['id']}] {step['action']}: {step['description']}")

    if data["assumptions_made"]:
        print("\n-- ASSUMPTIONS MADE --")
        for a in data["assumptions_made"]:
            print(f"  - {a}")

    print("\n-- REFLECTION --")
    print(f"  needs_retry: {data['reflection']['needs_retry']}")
    print(f"  issues: {data['reflection']['issues']}")
    print(f"  retried: {data['retried']}")

    print(f"\n-- OUTPUT --\n  {data['summary']}")
    print(f"  Download: {BASE_URL}{data['download_url']}")


if __name__ == "__main__":
    for name, text in TEST_CASES.items():
        run_case(name, text)
