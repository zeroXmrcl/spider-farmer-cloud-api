class SpiderFarmerError(Exception):
    """API, crypto, or transport failure.

    ``code`` is the cloud envelope code, an HTTP status, or one of
    ``parse``, ``decrypt``, ``network``, ``timeout``, ``tls``, ``dns``,
    ``mqtt``, ``topic``, ``write``.
    """

    def __init__(self, code: str, msg: str) -> None:
        self.code = code
        self.msg = msg
        super().__init__(f"{code} {msg}")
