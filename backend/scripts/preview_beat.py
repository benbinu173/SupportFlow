"""Celery beat with a renewable lease and no worker task-module imports."""

import logging
import os
import threading
from contextlib import suppress
from typing import Any

from celery.beat import PersistentScheduler
from redis import Redis

from app.workers.celery_app import celery_app

# Beat publishes task names and needs no task implementations. Loading worker
# AI/document modules here would duplicate their memory in another process.
celery_app.conf.include = []
logger = logging.getLogger(__name__)


class LeasedScheduler(PersistentScheduler):  # type: ignore[misc]
    """Defer dispatch while another preview owns the scheduler lease."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.client = Redis.from_url(
            str(celery_app.conf.broker_url), socket_timeout=5, socket_connect_timeout=5
        )
        self.lease = self.client.lock(
            "supportflow:preview:beat", timeout=30, blocking=False, thread_local=False
        )
        self.acquired = False
        self.stop_renewing = threading.Event()
        self.renewal: threading.Thread | None = None
        super().__init__(*args, **kwargs)

    def _renew(self) -> None:
        while not self.stop_renewing.wait(5):
            try:
                self.lease.extend(30, replace_ttl=True)
            except Exception:
                # A blocked publisher must stop before lease expiry permits
                # another beat. An exception in a thread would leave it alive.
                logger.error("Preview scheduler lost its lease; stopping")
                os._exit(1)

    def tick(self, *args: Any, **kwargs: Any) -> float:
        if not self.acquired:
            if not self.lease.acquire():
                return 2.0
            self.acquired = True
            self.renewal = threading.Thread(target=self._renew, daemon=True)
            self.renewal.start()
        return float(super().tick(*args, **kwargs))

    def close(self) -> None:
        super().close()
        self.stop_renewing.set()
        if self.renewal is not None:
            self.renewal.join(timeout=6)
        if self.acquired:
            with suppress(Exception):
                self.lease.release()
        self.client.close()
