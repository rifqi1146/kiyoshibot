

class UpstreamError(Exception):
    pass


class Blocked(UpstreamError):
    pass


class BadRequest(Exception):
    pass


class NotFound(Exception):
    pass
