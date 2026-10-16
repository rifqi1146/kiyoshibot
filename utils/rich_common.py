

def is_message_not_modified(error) -> bool:
    text = str(error or "").lower()
    return "not modified" in text
