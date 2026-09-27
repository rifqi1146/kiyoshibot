ASUPAN_CACHE = []
ASUPAN_KEYWORD_CACHE = {}
# Berapa request /asupan user yang sedang diproses. Warm (prefetch) berhenti
# kalau > 0 supaya tidak berebut kuota tikwm dengan request user.
ASUPAN_ACTIVE_USERS = 0
ASUPAN_MESSAGE_KEYWORD = {}
ASUPAN_FETCHING = False
ASUPAN_ENABLED_CHATS = set()
AUTODEL_ENABLED_CHATS = set()
ASUPAN_DELETE_JOBS = {}
ASUPAN_COOLDOWN = {}