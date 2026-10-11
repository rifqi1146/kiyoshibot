"""Constants for MovieBox Explorer UI (/moviebox)."""

LABEL = "MovieBox"
PREFIX = "mb"
BASE_URL = "https://officialmoviebox.com"
API_BASE = "https://h5-api.aoneroom.com/wefeed-h5api-bff"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 25
# Items shown per Telegram page.
PER_PAGE = 50
# Hard cap on collected results.
MAX_RESULTS = 200
# Sections shown per picker page.
SECTIONS_PER_PAGE = 12
CACHE_TTL = 3600
INDEX_TTL = 1800
EPISODES_PER_PAGE = 15

# ---------------------------------------------------------------------------
# Section catalogue
# ---------------------------------------------------------------------------
# kind:
#   "home"     -> read the pre-curated `operatingList` block titled `title`
#                 from `GET /home?host=<host>`.
#   "trending" -> `GET /subject/trending?[tabId=<tab>&]page=<N>&perPage=<M>`
#   "filter"   -> `POST /subject/filter` with the given body.
#
# key -> (label, kind, params)
SECTIONS: dict[str, tuple[str, str, dict]] = {
    # --- Trending ---------------------------------------------------------
    "trending":          ("Trending Now",           "trending", {}),
    "trending_movies":   ("Trending Movies",        "trending", {"tab": "ONEROOM_MOVIE"}),
    "trending_tv":       ("Trending TV Series",     "trending", {"tab": "ONEROOM_TVSHOW"}),
    # --- Popular ----------------------------------------------------------
    "popular_movies":    ("Popular Movies",         "home", {"title": "Popular Movie"}),
    "popular_series":    ("Popular Series",         "home", {"title": "Popular Series"}),
    # --- New / Upcoming ---------------------------------------------------
    "coming_soon":       ("Coming Soon",            "home", {"title": "Coming Soon"}),
    "hot_short_tv":      ("Hot Short TV",           "home", {"title": "🔥Hot Short TV"}),
    # --- Asian drama ------------------------------------------------------
    "kdrama":            ("K-Drama",                "home", {"title": "K-Drama"}),
    "cdrama":            ("C-Drama",                "home", {"title": "C-Drama"}),
    "indo_movies":       ("Indonesia Movies",      "filter", {"country": "Indonesia", "subjectType": "MOVIE", "sort": "HOT"}),
    "indo_series":       ("Indonesia Series",      "filter", {"country": "Indonesia", "subjectType": "TV", "sort": "HOT"}),
    "indo_latest":       ("Indonesia Latest",      "filter", {"country": "Indonesia", "sort": "LATEST"}),
    "korea_movies":      ("Korea Movies",          "filter", {"country": "Korea", "subjectType": "MOVIE", "sort": "HOT"}),
    "korea_series":      ("Korea Series",          "filter", {"country": "Korea", "subjectType": "TV", "sort": "HOT"}),
    "korea_latest":      ("Korea Latest",          "filter", {"country": "Korea", "sort": "LATEST"}),
    "china_movies":      ("China Movies",          "filter", {"country": "China", "subjectType": "MOVIE", "sort": "HOT"}),
    "china_series":      ("China Series",          "filter", {"country": "China", "subjectType": "TV", "sort": "HOT"}),
    "china_latest":      ("China Latest",          "filter", {"country": "China", "sort": "LATEST"}),
    "japan_movies":      ("Japan Movies",          "filter", {"country": "Japan", "subjectType": "MOVIE", "sort": "HOT"}),
    "japan_series":      ("Japan Series",          "filter", {"country": "Japan", "subjectType": "TV", "sort": "HOT"}),
    "japan_latest":      ("Japan Latest",          "filter", {"country": "Japan", "sort": "LATEST"}),
    "thailand_series":   ("Thailand Series",       "filter", {"country": "Thailand", "subjectType": "TV", "sort": "HOT"}),
    "thailand_latest":   ("Thailand Latest",       "filter", {"country": "Thailand", "sort": "LATEST"}),
    "philippines_movies":("Philippines Movies",    "filter", {"country": "Philippines", "subjectType": "MOVIE", "sort": "HOT"}),
    "india_movies":      ("India Movies",          "filter", {"country": "India", "subjectType": "MOVIE", "sort": "HOT"}),
    "turkey_series":     ("Turkey Series",         "filter", {"country": "Turkey", "subjectType": "TV", "sort": "HOT"}),
    "usa_movies":        ("USA Movies",            "filter", {"country": "United States", "subjectType": "MOVIE", "sort": "HOT"}),
    "uk_series":         ("UK Series",             "filter", {"country": "United Kingdom", "subjectType": "TV", "sort": "HOT"}),
    # --- Anime ------------------------------------------------------------
    "anime_dub":         ("Anime [English Dubbed]", "home", {"title": "Anime[English Dubbed]"}),
    "anime_filter":      ("Anime & Animation",      "filter", {"genre": "Animation", "sort": "HOT"}),
    "adult_animation":   ("Adult Animation",        "home", {"title": "Adult Animation"}),
    # --- Movies by genre --------------------------------------------------
    "action_movies":     ("Action Movies",          "home", {"title": "Action Movies"}),
    "horror_movies":     ("Horror Movies",          "home", {"title": "Horror Movies"}),
    "romantic_movie":    ("Romantic Movies",        "filter", {"genre": "Romance", "subjectType": "MOVIE", "sort": "HOT"}),
    "comedy":            ("Comedy",                 "home", {"title": "Comedy"}),
    "epic_fantasy":      ("Epic Fantasy",           "home", {"title": "Epic Fantasy"}),
    "teen_fantasy":      ("Teen Fantasy",           "home", {"title": "Teen Fantasy"}),
    "teen_romance":      ("Teen Romance",           "home", {"title": "Teen Romance"}),
    "gangster":          ("Gangster",               "home", {"title": "Gangster"}),
    "action_thriller":   ("Action & Thriller",      "home", {"title": "Action&Thriller"}),
    "superhero":         ("Superhero Series",       "home", {"title": "Superhero Series"}),
    "bl_story":          ("BL Story",               "home", {"title": "BL Story"}),
    "nollywood":         ("Nollywood",              "home", {"title": "Nollywood Movie"}),
    "black_shows":       ("Black Shows",             "home", {"title": "Must-watch Black Shows"}),
    "bet_plus":          ("BET+",                   "home", {"title": "Bet+"}),
    "romance":           ("Romance",                "home", {"title": "💓Romance 💓"}),
    # --- Genre filters (API) ----------------------------------------------
    "genre_action":      ("Genre: Action",          "filter", {"genre": "Action", "sort": "HOT"}),
    "genre_romance":     ("Genre: Romance",         "filter", {"genre": "Romance", "sort": "HOT"}),
    "genre_comedy":      ("Genre: Comedy",          "filter", {"genre": "Comedy", "sort": "HOT"}),
    "genre_horror":      ("Genre: Horror",          "filter", {"genre": "Horror", "sort": "HOT"}),
    "genre_drama":       ("Genre: Drama",           "filter", {"genre": "Drama", "sort": "HOT"}),
    "genre_thriller":    ("Genre: Thriller",        "filter", {"genre": "Thriller", "sort": "HOT"}),
    "genre_fantasy":     ("Genre: Fantasy",         "filter", {"genre": "Fantasy", "sort": "HOT"}),
    "genre_scifi":       ("Genre: Sci-Fi",          "filter", {"genre": "Sci-Fi", "sort": "HOT"}),
    "genre_crime":       ("Genre: Crime",           "filter", {"genre": "Crime", "sort": "HOT"}),
    "genre_animation":   ("Genre: Animation",       "filter", {"genre": "Animation", "sort": "HOT"}),
    # --- Recent -----------------------------------------------------------
    "recent_movies":     ("Recent Movies",          "filter", {"subjectType": "MOVIE", "sort": "LATEST"}),
    "recent_series":     ("Recent Series",          "filter", {"subjectType": "TV", "sort": "LATEST"}),
    "recent_all":        ("Recent Added",           "filter", {"sort": "LATEST"}),
}

# Main menu layout: rows of section keys. "search"/"close"/"all"/"countries"
# are special actions handled by the renderer.
MENU_LAYOUT: tuple[tuple[str, ...], ...] = (
    ("search", "trending"),
    ("trending_movies", "trending_tv"),
    ("coming_soon", "hot_short_tv"),
    ("indo_movies", "indo_series"),
    ("kdrama", "cdrama"),
    ("recent_all", "popular_movies"),
    ("popular_series", "anime_dub"),
    ("popular", "all"),
    ("close",),
)

# Curated quick-search titles (index-based callbacks keep data short).
POPULAR_TITLES: tuple[str, ...] = (
    "Avatar",
    "Stranger Things",
    "Squid Game",
    "Lucifer",
    "Prison Break",
    "The Rookie",
    "Wednesday",
    "The Boys",
    "One Piece",
    "Money Heist",
    "Breaking Bad",
    "Game of Thrones",
)
