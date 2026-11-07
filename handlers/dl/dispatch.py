import inspect
import time
import logging

from handlers.dl.stages import stage
from handlers.dl.utils import FileSizeLimitExceeded

log = logging.getLogger(__name__)


class Route:
    __slots__ = ("matcher", "handler", "label", "params", "soft")

    def __init__(self, matcher, handler, label, soft=False):
        self.matcher = matcher
        self.handler = handler
        self.label = label
        self.soft = soft
        sig = inspect.signature(handler)
        self.params = None if any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        ) else frozenset(sig.parameters)

    def build_kwargs(self, kwargs):
        if self.params is None:
            return kwargs
        out = {k: v for k, v in kwargs.items() if k in self.params}
        if "url" in self.params and "url" not in out and "raw_url" in kwargs:
            out["url"] = kwargs["raw_url"]
        return out

    def matches(self, url):
        try:
            return bool(self.matcher(url))
        except Exception:
            return False


async def dispatch(raw_url, routes, kwargs, on_soft_failure=None):
    for route in routes:
        if not route.matches(raw_url):
            continue
        t0 = time.monotonic()
        try:
            result = await route.handler(**route.build_kwargs(kwargs))
        except FileSizeLimitExceeded:
            raise
        except Exception as e:
            if not route.soft:
                raise
            if on_soft_failure:
                on_soft_failure(route, raw_url, e)
            continue
        stage(f"scrape+download:{route.label}", t0, job=raw_url)
        return result
    return None
