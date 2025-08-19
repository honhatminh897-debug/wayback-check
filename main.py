import os
import io
import re
import asyncio
import logging
import time
import random
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlsplit

import aiohttp
import pandas as pd
from dateutil import tz
from telegram import Update, constants
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ========= Config =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE = tz.gettz("Asia/Bangkok")
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "10"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "3"))
ALERT_AGE_YEARS = float(os.getenv("ALERT_AGE_YEARS", "5"))
PROGRESS_THROTTLE_SEC = float(os.getenv("PROGRESS_THROTTLE_SEC", "2"))
CDX_RPS = float(os.getenv("CDX_RPS", "2"))  # requests per second
CDX_JITTER_MS = int(os.getenv("CDX_JITTER_MS", "250"))
SHARDS = int(os.getenv("SHARDS", "4"))  # số phần chia nhỏ list để chạy song song

CDX_BASE = "https://web.archive.org/cdx/search/cdx?output=json&fl=timestamp,original&collapse=digest&sort=asc&from=1996"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("wayback-age-bot")

# ========= Global rate limiter =========
class RateLimiter:
    def __init__(self, rps: float):
        self.rps = max(0.1, rps)
        self.interval = 1.0 / self.rps
        self._dq = deque()
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            while self._dq and now - self._dq[0] > 1.0:
                self._dq.popleft()
            if len(self._dq) >= int(self.rps):
                to_sleep = max(0.0, 1.0 - (now - self._dq[0]))
                await asyncio.sleep(to_sleep)
                now = time.monotonic()
                while self._dq and now - self._dq[0] > 1.0:
                    self._dq.popleft()
            self._dq.append(time.monotonic())
            if CDX_JITTER_MS > 0:
                await asyncio.sleep(random.uniform(0, CDX_JITTER_MS / 1000.0))

rate_limiter = RateLimiter(CDX_RPS)

# ========= Helpers =========
def _normalize_domain(raw: str) -> str:
    s = raw.strip()
    if not s:
        return ""
    if re.match(r"^[a-zA-Z]+://", s):
        s = urlsplit(s).netloc or s
    s = s.split("/")[0]
    s = s.split(":")[0]
    if s.startswith("www."):
        s = s[4:]
    return s.lower().strip(".")

def _years_between(d1: datetime, d2: datetime) -> float:
    return (d2 - d1).days / 365.2425

async def _http_get_json_or_text(session: aiohttp.ClientSession, url: str):
    await rate_limiter.wait()
    headers = {"Accept": "application/json, */*",
               "User-Agent": "WaybackAgeBot/1.0 (+https://railway.app)"}
    async with session.get(url, timeout=REQUEST_TIMEOUT, headers=headers) as resp:
        if resp.status == 429:
            retry_after = resp.headers.get("Retry-After")
            sleep_s = 2.0
            if retry_after:
                try:
                    sleep_s = max(sleep_s, float(retry_after))
                except Exception:
                    pass
            sleep_s += random.uniform(0, 1.5)
            await asyncio.sleep(sleep_s)
            raise aiohttp.ClientResponseError(
                request_info=resp.request_info, history=resp.history,
                status=429, message=f"HTTP 429 after wait {sleep_s:.1f}s"
            )
        if resp.status in (502, 503, 504):
            raise aiohttp.ClientResponseError(
                request_info=resp.request_info, history=resp.history,
                status=resp.status, message=f"Transient HTTP {resp.status}"
            )
        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "")
        if "json" in ct.lower():
            try:
                return await resp.json(content_type=None)
            except Exception:
                pass
        return await resp.text()

async def _cdx_attempt(session: aiohttp.ClientSession, url: str, retries: int = 5):
    backoff = 1.0
    last_err = None
    for _ in range(retries):
        try:
            return await _http_get_json_or_text(session, url)
        except Exception as e:
            last_err = e
            await asyncio.sleep(backoff + random.uniform(0, 0.5))
            backoff = min(backoff * 2, 16)
    logger.warning("CDX request failed after retries: %r", last_err)
    return None

async def fetch_counts(session: aiohttp.ClientSession, domain: str) -> tuple[int | None, int | None]:
    base = "https://web.archive.org/cdx/search/cdx?output=json&showNumPages=true&pageSize=1"
    q = f"&matchType=prefix&url={domain}/*"
    total_payload = await _cdx_attempt(session, base + q)
    total = _parse_num_pages(total_payload)
    ok_payload = await _cdx_attempt(session, base + q + "&filter=statuscode:200")
    ok = _parse_num_pages(ok_payload)
    return total, ok

def _parse_num_pages(payload) -> int | None:
    if isinstance(payload, dict) and "numPages" in payload:
        try:
            return int(payload["numPages"])
        except Exception:
            return None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict) and "numPages" in payload[0]:
        try:
            return int(payload[0]["numPages"])
        except Exception:
            return None
    if isinstance(payload, str):
        import re as _re
        m = _re.search(r'numPages\s*:\s*(\d+)', payload)
        if m:
            return int(m.group(1))
    return None

async def fetch_earliest_snapshot(session: aiohttp.ClientSession, domain: str, semaphore: asyncio.Semaphore) -> dict:
    result = {"domain": domain, "earliest_snapshot": None, "age_years": None,
              "snapshots_total": None, "snapshots_200": None, "error": None}

    attempts = [
        f"{CDX_BASE}&matchType=host&url={domain}/*&filter=statuscode:200&limit=1",
        f"{CDX_BASE}&matchType=prefix&url={domain}/*&filter=statuscode:200&limit=1",
        f"{CDX_BASE}&matchType=prefix&url={domain}/*&limit=1",
    ]

    for url in attempts:
        async with semaphore:
            payload = await _cdx_attempt(session, url)
        if isinstance(payload, list) and len(payload) >= 2 and len(payload[1]) >= 1:
            ts = payload[1][0]
            if ts:
                try:
                    dt = datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                    result["earliest_snapshot"] = dt.astimezone(TIMEZONE).strftime("%Y-%m-%d")
                    result["age_years"] = round(_years_between(dt, datetime.now(timezone.utc)), 2)
                except Exception as e:
                    result["error"] = f"parse_ts: {repr(e)}"
            break

    try:
        total, ok = await fetch_counts(session, domain)
        result["snapshots_total"] = total
        result["snapshots_200"] = ok
    except Exception as e:
        if result["error"]:
            result["error"] += f"; count: {repr(e)}"
        else:
            result["error"] = f"count: {repr(e)}"

    if result["earliest_snapshot"] is None and result["error"] is None:
        result["error"] = "cdx_failed_or_no_data"

    return result

def _progress_bar(done: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "[....................]"
    filled = int(width * done / total)
    return "[" + "█" * filled + "·" * (width - filled) + "]"

# ========= Core processing =========
async def process_domains(domains: list[str], update: Update, context: ContextTypes.DEFAULT_TYPE, shard_id: int) -> list[dict]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    results = []
    started = time.time()
    cdx429 = 0

    async def one(domain: str):
        nonlocal cdx429
        try:
            return await fetch_earliest_snapshot(session, domain, semaphore)
        except aiohttp.ClientResponseError as e:
            if e.status == 429:
                cdx429 += 1
            return {"domain": domain, "earliest_snapshot": None, "age_years": None,
                    "snapshots_total": None, "snapshots_200": None, "error": f"http_error:{e.status}"}
        except Exception as e:
            return {"domain": domain, "earliest_snapshot": None, "age_years": None,
                    "snapshots_total": None, "snapshots_200": None, "error": f"exc:{repr(e)}"}

    async with aiohttp.ClientSession() as session:
        for i in range(0, len(domains), BATCH_SIZE):
            batch = domains[i:i+BATCH_SIZE]
            batch_results = await asyncio.gather(*[one(d) for d in batch])
            results.extend(batch_results)

    logger.info(f"Shard {shard_id} done: {len(results)} domains, 429={cdx429}")
    return results

async def process_file_with_shards(update: Update, context: ContextTypes.DEFAULT_TYPE, file_bytes: bytes, filename: str) -> bytes:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("xlsx", "xls"):
        df = pd.read_excel(io.BytesIO(file_bytes), header=None)
    else:
        df = pd.read_csv(io.BytesIO(file_bytes), header=None)

    domains = []
    for val in df.iloc[:, 0].astype(str).tolist():
        n = _normalize_domain(val)
        if n:
            domains.append(n)
    domains = list(dict.fromkeys(domains))

    if not domains:
        await context.bot.send_message(update.effective_chat.id, "😿 Không tìm thấy domain hợp lệ trong file.")
        return None

    # chia nhỏ list
    parts = [domains[i::SHARDS] for i in range(SHARDS) if domains[i::SHARDS]]

    await context.bot.send_message(update.effective_chat.id,
        f"🔍 Bắt đầu crawl Wayback — Tổng {len(domains)} domain, chia thành {len(parts)} shard song song.")

    tasks = [asyncio.create_task(process_domains(part, update, context, idx+1)) for idx, part in enumerate(parts)]
    all_results = await asyncio.gather(*tasks)

    # gộp kết quả
    results = [r for sub in all_results for r in sub]

    cols = ["domain", "earliest_snapshot", "age_years", "snapshots_total", "snapshots_200", "error"]
    result_df = pd.DataFrame(results)[cols]
    result_df.sort_values(by=["age_years"], ascending=[False], inplace=True, na_position="last")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        result_df.to_excel(writer, index=False, sheet_name="wayback_age")
        summary = pd.DataFrame({
            "total_domains": [len(domains)],
            "found_snapshots": [result_df['earliest_snapshot'].notna().sum()],
            "no_snapshot": [result_df['earliest_snapshot'].isna().sum()],
            "avg_snapshots_total": [result_df['snapshots_total'].dropna().mean() if result_df['snapshots_total'].notna().any() else None],
            "avg_snapshots_200": [result_df['snapshots_200'].dropna().mean() if result_df['snapshots_200'].notna().any() else None],
            "generated_at": [datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")],
            "settings": [f"RPS={CDX_RPS}, CONC={MAX_CONCURRENCY}, BATCH={BATCH_SIZE}, SHARDS={SHARDS}"]
        })
        summary.to_excel(writer, index=False, sheet_name="summary")
    buf.seek(0)
    return buf.read()

# ========= Handlers =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Chào bạn! 👋 Gửi file Excel/CSV có 1 cột domain.\n"
        "Bot sẽ crawl Wayback song song theo shards để tính tuổi domain, snapshots.\n\n"
        "Biến môi trường gợi ý:\n"
        "- CDX_RPS=2~3\n- MAX_CONCURRENCY=20~40\n- SHARDS=4"
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if not doc:
        return
    filename = doc.file_name or "input.xlsx"
    tg_file = await context.bot.get_file(doc.file_id)
    file_bytes = await tg_file.download_as_bytearray()

    try:
        result_bytes = await process_file_with_shards(update, context, file_bytes, filename)
        if not result_bytes:
            return
    except Exception as e:
        logger.exception("Process error")
        await update.message.reply_text(f"❌ Lỗi xử lý file: {repr(e)}")
        return

    out_name = f"wayback_age_{datetime.now(TIMEZONE).strftime('%Y%m%d_%H%M%S')}.xlsx"
    await update.message.reply_document(
        document=result_bytes,
        filename=out_name,
        caption="✅ Crawl hoàn tất! Báo cáo đầy đủ đã sẵn sàng."
    )

def main() -> None:
    token = BOT_TOKEN
    if not token:
        raise RuntimeError("Missing BOT_TOKEN env var")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
