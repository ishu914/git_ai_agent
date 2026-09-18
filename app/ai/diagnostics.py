from app.ai.orchestrator import AIOrchestrator


def main() -> None:
    inventory = AIOrchestrator().inventory()
    print(f"OpenRouter API configured: {'YES' if inventory['openrouter_configured'] else 'NO'}")
    print(f"Discovered OpenRouter free models: {len(inventory['openrouter'])}")
    for candidate in inventory["openrouter"]:
        print(
            f"- {candidate['model_id']} | json={candidate['supports_json']} "
            f"| code_review={candidate['supports_code_review']} | context={candidate['context_length'] or 'unknown'}"
        )
    print(f"Groq API configured: {'YES' if inventory['groq_configured'] else 'NO'}")
    print(f"Discovered Groq models: {len(inventory['groq'])}")
    for candidate in inventory["groq"]:
        print(
            f"- {candidate['model_id']} | json={candidate['supports_json']} "
            f"| code_review={candidate['supports_code_review']} | context={candidate['context_length'] or 'unknown'}"
        )
    print("Fallback order: OpenRouter eligible models, then Groq eligible models.")


if __name__ == "__main__":
    main()
