"""Entrypoint: `python -m multi_google_mcp`."""

from __future__ import annotations

import logging
import os
import re

import uvicorn

from .config import Settings


class _RedactQuery(logging.Filter):
    """Strip query strings (OAuth codes/state) from access log lines."""

    _re = re.compile(r"\?[^\s\"]*")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.args, tuple):
            record.args = tuple(self._re.sub("?…", a) if isinstance(a, str) else a for a in record.args)
        return True


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("uvicorn.access").addFilter(_RedactQuery())

    from .server import create_app

    settings = Settings.from_env()
    app, _, _ = create_app(settings)
    uvicorn.run(app, host="0.0.0.0", port=settings.port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
