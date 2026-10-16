def build_rag_prompt(user_prompt: str, contexts: list[str]) -> str:
    if not contexts:
        return user_prompt
    context_text = "\n\n".join(contexts)
    return f"<bot_reference>\n{context_text}\n</bot_reference>\n\n{user_prompt}"