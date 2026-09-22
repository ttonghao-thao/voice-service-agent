import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.contracts import DomainError, uid
from redis.asyncio import Redis


@dataclass
class Lease:
    token: str
    touched: float


class Coordination:
    """Per-conversation ownership; PostgreSQL epoch changes fence each newly acquired lease.

    Redis owns routing/TTL, while the DB owns durable epoch/turn commits. WebSocket state
    stays on its gateway. A load balancer must keep a browser on one gateway.
    """

    def __init__(self, settings):
        self.settings = settings
        self.redis = Redis.from_url(settings.redis_url) if settings.redis_url else None
        self.valid = True
        self.buckets = OrderedDict()
        self.leases: dict[str, Lease] = {}
        self.task = None

    @staticmethod
    def key(cid):
        return "voice-service:owner:" + cid

    async def start(self):
        if self.redis:
            await self.redis.ping()
        self.task = asyncio.create_task(self.renew())

    async def acquire(self, cid):
        await self.check()
        lease = self.leases.get(cid)
        if lease:
            await self.check(cid)
            return False
        token = uid()
        if self.redis and not await self.redis.set(self.key(cid), token, nx=True, ex=15):
            raise DomainError(
                "SESSION_OWNED_BY_OTHER", "This conversation is active on another connection. Keep that connection or try later.", 409, True
            )
        self.leases[cid] = Lease(token, time.monotonic())
        return True

    async def renew(self):
        try:
            while True:
                await asyncio.sleep(4)
                for cid, lease in list(self.leases.items()):
                    if time.monotonic() - lease.touched > 60:
                        await self.release(cid)
                        continue
                    if not self.redis:
                        continue
                    ok = await self.redis.eval(
                        "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('expire',KEYS[1],15) else return 0 end",
                        1,
                        self.key(cid),
                        lease.token,
                    )
                    if not ok:
                        self.leases.pop(cid, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Redis failure fails all active output closed; maintenance closes resources.
            self.valid = False

    async def check(self, cid=None):
        if not self.valid:
            raise DomainError("GATEWAY_LEASE_LOST", "Session ownership expired. Reconnect.", 503, True)
        if cid is not None:
            lease = self.leases.get(cid)
            if lease is None:
                raise DomainError("GATEWAY_LEASE_LOST", "Session ownership expired. Reconnect.", 503, True)
            if self.redis and await self.redis.get(self.key(cid)) != lease.token.encode():
                self.leases.pop(cid, None)
                raise DomainError("GATEWAY_LEASE_LOST", "Session ownership expired. Reconnect.", 503, True)
            lease.touched = time.monotonic()

    async def release(self, cid):
        lease = self.leases.pop(cid, None)
        if self.redis and lease:
            await self.redis.eval(
                "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end",
                1,
                self.key(cid),
                lease.token,
            )

    async def rate_limit(self, key, limit):
        minute = int(time.time() // 60)
        if self.redis:
            k = f"voice-service:rate:{key}:{minute}"
            count = await self.redis.eval(
                "local n=redis.call('incr',KEYS[1]); if n==1 then redis.call('expire',KEYS[1],70) end; return n",
                1,
                k,
            )
        else:
            count, previous = self.buckets.get(key, (0, minute))
            count = count + 1 if previous == minute else 1
            self.buckets[key] = (count, minute)
            if len(self.buckets) > 10000:
                self.buckets.popitem(last=False)
        if count > limit:
            raise DomainError("RATE_LIMITED", "Too many requests. Please try again later.", 429, True)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.redis:
            try:
                for cid in list(self.leases):
                    await self.release(cid)
            finally:
                await self.redis.aclose()
