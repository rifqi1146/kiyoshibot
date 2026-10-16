from .commands import make_site_cmd
from .callbacks import becekku_callback, lendirqu_callback

becekku_cmd = make_site_cmd("becekku")
lendirqu_cmd = make_site_cmd("lendirqu")

__all__ = ["becekku_cmd", "lendirqu_cmd", "becekku_callback", "lendirqu_callback"]
