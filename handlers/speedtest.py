import os,json,html,uuid,shutil,asyncio,logging,subprocess
from telegram import Update
from telegram.ext import ContextTypes
from utils.config import OWNER_ID
from handlers.dl.proc import spawn_process, PIPE, resolve_aria2

log=logging.getLogger(__name__)
TMP_DIR=os.getenv("TMP_DIR","downloads")
SPEEDTEST_TIMEOUT=int(os.getenv("SPEEDTEST_TIMEOUT","180"))
SPEEDTEST_IMAGE_TIMEOUT=int(os.getenv("SPEEDTEST_IMAGE_TIMEOUT","45"))

def _result_png_url(url:str)->str:
    url=str(url or "").strip()
    if not url:
        raise RuntimeError("Speedtest result URL not found.")
    url=url.split("#",1)[0].split("?",1)[0].rstrip("/")
    if not url.endswith(".png"):
        url=f"{url}.png"
    return url

def run_speedtest()->dict:
    p=subprocess.run(
        ["speedtest","--accept-license","--accept-gdpr","-f","json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=SPEEDTEST_TIMEOUT,
    )
    if p.returncode!=0:
        raise RuntimeError(f"Speedtest failed: {p.stderr.strip() or p.stdout.strip()}")
    out=(p.stdout or "").strip()
    start=out.find("{")
    end=out.rfind("}")+1
    if start<0 or end<=0:
        raise RuntimeError("Invalid speedtest output, JSON not found.")
    try:
        return json.loads(out[start:end])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Speedtest JSON parse error: {e}")

async def _download_with_aria2c(url:str,out_path:str):
    aria2c=resolve_aria2()
    if not aria2c:
        raise RuntimeError("aria2c is not installed.")
    os.makedirs(os.path.dirname(out_path) or ".",exist_ok=True)
    cmd=[
        aria2c,
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--summary-interval=0",
        "--console-log-level=warn",
        "-x","8",
        "-s","8",
        "-k","1M",
        "-o",os.path.basename(out_path),
        "-d",os.path.dirname(out_path),
        url,
    ]
    proc=await spawn_process(*cmd,stdout=PIPE,stderr=PIPE)
    try:
        stdout,stderr=await asyncio.wait_for(proc.communicate(),timeout=SPEEDTEST_IMAGE_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError("Speedtest image download timeout.")
    if proc.returncode!=0:
        err=(stderr.decode(errors="ignore") or stdout.decode(errors="ignore") or f"aria2c exited with code {proc.returncode}").strip()
        raise RuntimeError(err[-1000:])
    if not os.path.exists(out_path) or os.path.getsize(out_path)<=0:
        raise RuntimeError("Downloaded speedtest image is empty.")

async def _download_result_image(url:str)->str:
    os.makedirs(TMP_DIR,exist_ok=True)
    out_path=os.path.join(TMP_DIR,f"speedtest_{uuid.uuid4().hex}.png")
    last_err=None
    for attempt in range(1,4):
        try:
            await _download_with_aria2c(url,out_path)
            return out_path
        except Exception as e:
            last_err=e
            log.warning("Speedtest image download failed | attempt=%s url=%s err=%r",attempt,url,e)
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except Exception:
                pass
            await asyncio.sleep(2)
    raise RuntimeError(f"Failed to download speedtest image: {last_err}")

async def speedtest_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    msg=update.effective_message
    user=update.effective_user
    if not msg or not user:
        return
    if user.id not in OWNER_ID:
        return
    status=await msg.reply_text("<b>Running Speedtest...</b>",parse_mode="HTML")
    image_path=None
    try:
        data=await asyncio.to_thread(run_speedtest)
        result_url=((data.get("result") or {}).get("url") or "").strip()
        png_url=_result_png_url(result_url)
        image_path=await _download_result_image(png_url)
        with open(image_path,"rb") as f:
            await msg.reply_photo(photo=f,reply_to_message_id=msg.message_id)
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        err=html.escape(str(e) or repr(e))[:3500]
        try:
            await status.edit_text(f"<b>Speedtest failed</b>\n\n<code>{err}</code>",parse_mode="HTML")
        except Exception:
            pass
    finally:
        try:
            if image_path and os.path.exists(image_path):
                os.remove(image_path)
                log.info("Speedtest image deleted | file=%s",image_path)
        except Exception as e:
            log.warning("Failed to delete speedtest image | file=%s err=%r",image_path,e)