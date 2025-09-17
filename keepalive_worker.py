import os, asyncio, httpx
from datetime import datetime, timedelta
import pytz

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
KEEPALIVE_INTERVAL_MS = int(os.getenv("KEEPALIVE_INTERVAL_MS") or 600000)  # 10 min

async def keepalive_loop():
    if not RENDER_EXTERNAL_URL:
        print("⚠️ No RENDER_EXTERNAL_URL set, skipping keepalive")
        return

    ist = pytz.timezone("Asia/Kolkata")

    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{RENDER_EXTERNAL_URL}/test/selfcheck")
            next_ping = datetime.now(ist) + timedelta(milliseconds=KEEPALIVE_INTERVAL_MS)
            print(f"🔄 Keepalive ping {r.status_code} | next @ {next_ping.strftime('%I:%M:%S %p')}")
        except Exception as e:
            print(f"⚠️ Keepalive ping failed: {e}")
        await asyncio.sleep(KEEPALIVE_INTERVAL_MS / 1000.0)


async def main():
    # Start keepalive in the background
    asyncio.create_task(keepalive_loop())

    # Keep process alive (replace with your actual app runner if needed)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
