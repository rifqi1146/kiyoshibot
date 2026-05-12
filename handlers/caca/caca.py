import re,os,uuid,base64,shutil,asyncio,random,html,logging,mimetypes,subprocess,aiohttp
from typing import Optional
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes
from handlers.join import require_join_or_block
from handlers.gsearch import google_search
from utils.text import split_message,sanitize_ai_output
from .caca_prompt import PERSONAS
from utils.http import get_http_session
from database import caca_db
from utils import caca_memory
from utils.config import CLOUDFLARE_ACCOUNT_ID,CLOUDFLARE_AUTH_TOKEN,CLOUDFLARE_MODEL

logger=logging.getLogger(__name__)
CLOUDFLARE_TIMEOUT=int(os.getenv("CLOUDFLARE_TIMEOUT","60"))
TMP_DIR=os.getenv("TMP_DIR","downloads")
CACA_IMAGE_MAX_SIZE=int(os.getenv("CACA_IMAGE_MAX_SIZE",str(5*1024*1024)))
_EMOS=["🌸","💖","🧸","🎀","🌟","💫"]
_URL_RE=re.compile(r"(https?://[^\s'\"<>]+)",re.I)

def _emo():
    return random.choice(_EMOS)

def _parse_html(html_text:str)->Optional[str]:
    soup=BeautifulSoup(html_text,"html.parser")
    for t in soup(["script","style","iframe","noscript"]):
        t.decompose()
    ps=[p.get_text(" ",strip=True) for p in soup.find_all("p") if len(p.text)>30]
    return ("\n\n".join(ps))[:12000] or None

def _cleanup_memory():
    try:
        asyncio.get_event_loop().create_task(caca_memory.cleanup())
    except Exception as e:
        logger.warning("Failed to schedule Caca memory cleanup | err=%r",e)

def _get_thread_id(msg):
    thread_id=getattr(msg,"message_thread_id",None)
    if thread_id:
        return thread_id
    reply=getattr(msg,"reply_to_message",None)
    if reply:
        return getattr(reply,"message_thread_id",None)
    return None

async def _typing_loop(bot, chat_id, stop_event: asyncio.Event, message_thread_id=None):
    try:
        kwargs = {
            "chat_id": chat_id,
            "action": ChatAction.TYPING,
        }
        if message_thread_id:
            kwargs["message_thread_id"] = message_thread_id
        while not stop_event.is_set():
            try:
                await bot.send_chat_action(**kwargs)
            except Exception as api_err:
                log.warning("Typing action gagal, hapus thread_id. Error: %s", api_err)
                kwargs.pop("message_thread_id", None)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        log.debug("Caca typing task cancelled")
    except Exception as e:
        log.warning("Caca typing loop stopped | err=%r", e)

async def _stop_typing_task(stop,typing):
    if stop:
        stop.set()
    if typing:
        typing.cancel()
        try:
            await typing
        except asyncio.CancelledError:
            logger.debug("Caca typing task stopped")
        except Exception as e:
            logger.warning("Caca typing task stop failed | err=%r",e)

async def _reply_thread(bot,msg,text,parse_mode=None):
    thread_id=_get_thread_id(msg)
    kwargs={
        "chat_id":msg.chat_id,
        "text":text,
        "parse_mode":parse_mode,
        "reply_to_message_id":msg.message_id,
    }
    if thread_id:
        kwargs["message_thread_id"]=thread_id
    try:
        return await bot.send_message(**kwargs)
    except Exception as e:
        logger.warning("Caca reply failed, retry without reply target | chat_id=%s thread_id=%s err=%r",msg.chat_id,thread_id,e)
        kwargs.pop("reply_to_message_id",None)
        try:
            return await bot.send_message(**kwargs)
        except Exception as e2:
            logger.warning("Caca reply retry failed, retry without thread | chat_id=%s thread_id=%s err=%r",msg.chat_id,thread_id,e2)
            kwargs.pop("message_thread_id",None)
            return await bot.send_message(**kwargs)

def _normalize_caca_output(text:str)->str:
    text=html.unescape(text or "")
    text=text.replace("\r\n","\n").replace("\r","\n")
    text=re.sub(r"<[^>\n]+>"," ",text)
    text=re.sub(r"\b/?(?:b|i|u|s|code|pre|strong|em|tg-spoiler)\b"," ",text,flags=re.I)
    text=re.sub(r"&[a-zA-Z#0-9]+;"," ",text)
    text=re.sub(r"[ \t]+"," ",text)
    text=re.sub(r"\n\s*\n+","\n",text)
    lines=[]
    for line in text.split("\n"):
        line=line.strip()
        if not line:
            continue
        line=re.sub(r"\s+"," ",line)
        if (
            lines
            and len(lines[-1])<=35
            and not re.search(r"[.!?:]$",lines[-1])
        ):
            lines[-1]+=f" {line}"
        else:
            lines.append(line)
    text="\n".join(lines)
    text=re.sub(r"\s{2,}"," ",text)
    return text.strip()

def _strip_thinking_leak(text:str)->str:
    text=str(text or "").strip()
    text=re.sub(r"(?is)<think>.*?</think>","",text).strip()
    text=re.sub(r"(?is)<thinking>.*?</thinking>","",text).strip()
    leak=any(k in text.lower() for k in (
        "wait, looking at","looking at the context","let me ","i should ","the user wants",
        "as caca","actually,","or more","this feels right","let me revise"
    ))
    if leak:
        quoted=re.findall(r"[\"“]([^\"”]{8,2500})[\"”]",text,flags=re.S)
        if quoted:
            return quoted[-1].strip()
        lines=[]
        for line in text.splitlines():
            low=line.lower().strip()
            if not low:
                continue
            if low.startswith(("wait,","actually,","let me","the user","i should","looking at","or more","this feels")):
                continue
            if "as caca" in low or "the rules say" in low:
                continue
            lines.append(line)
        text="\n".join(lines).strip()
    return text

def _is_image_mime(mime:str)->bool:
    return str(mime or "").lower().startswith("image/")

def _guess_ext(mime:str,fallback:str=".jpg")->str:
    mime=str(mime or "").lower()
    if "png" in mime:
        return ".png"
    if "webp" in mime:
        return ".webp"
    if "jpeg" in mime or "jpg" in mime:
        return ".jpg"
    return fallback

def _guess_content_type(path:str)->str:
    mime,_=mimetypes.guess_type(path)
    mime=str(mime or "").lower()
    if mime.startswith("image/"):
        return mime
    ext=os.path.splitext(path)[1].lower()
    if ext in (".jpg",".jpeg"):
        return "image/jpeg"
    if ext==".png":
        return "image/png"
    if ext==".webp":
        return "image/webp"
    return "image/jpeg"

def _tmp_path(prefix:str,ext:str=".jpg")->str:
    ext=ext if str(ext or "").startswith(".") else f".{ext}"
    os.makedirs(TMP_DIR,exist_ok=True)
    return os.path.join(TMP_DIR,f"{prefix}_{uuid.uuid4().hex}{ext}")

def _convert_image_to_jpg(src_path:str)->str:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required to process images.")
    out_path=_tmp_path("caca_vision",".jpg")
    result=subprocess.run(
        ["ffmpeg","-y","-i",src_path,"-frames:v","1","-q:v","3",out_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode!=0 or not os.path.exists(out_path) or os.path.getsize(out_path)<=0:
        raise RuntimeError("Failed to convert image to JPG.")
    return out_path

def _image_to_data_url(path:str,mime:str="image/jpeg")->str:
    if not path or not os.path.exists(path):
        raise RuntimeError("Image file does not exist.")
    size=os.path.getsize(path)
    if size<=0:
        raise RuntimeError("Image file is empty.")
    if size>CACA_IMAGE_MAX_SIZE:
        raise RuntimeError(f"Image is too large. Max size is {CACA_IMAGE_MAX_SIZE//1024//1024}MB.")
    with open(path,"rb") as f:
        encoded=base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{encoded}"

async def _download_visual_from_message(bot,source_msg):
    if not source_msg:
        return None,None
    os.makedirs(TMP_DIR,exist_ok=True)
    if source_msg.photo:
        tg_file=await bot.get_file(source_msg.photo[-1].file_id)
        path=_tmp_path("caca_vision",".jpg")
        await tg_file.download_to_drive(path)
        return path,"image/jpeg"
    if source_msg.document:
        doc=source_msg.document
        if not _is_image_mime(doc.mime_type):
            return None,None
        if doc.file_size and doc.file_size>CACA_IMAGE_MAX_SIZE:
            raise RuntimeError(f"Image is too large. Max size is {CACA_IMAGE_MAX_SIZE//1024//1024}MB.")
        tg_file=await bot.get_file(doc.file_id)
        ext=os.path.splitext(doc.file_name or "")[1] or _guess_ext(doc.mime_type)
        path=_tmp_path("caca_vision",ext)
        await tg_file.download_to_drive(path)
        return path,doc.mime_type or _guess_content_type(path)
    if source_msg.sticker:
        sticker=source_msg.sticker
        if sticker.is_animated or sticker.is_video:
            raise RuntimeError("Animated/video stickers are not supported yet. Use a static sticker.")
        tg_file=await bot.get_file(sticker.file_id)
        path=_tmp_path("caca_vision",".webp")
        await tg_file.download_to_drive(path)
        return path,"image/webp"
    return None,None

async def _extract_visual_data_url(bot,msg):
    image_path=None
    converted_path=None
    try:
        image_path,image_mime=await _download_visual_from_message(bot,msg)
        if not image_path and msg.reply_to_message:
            image_path,image_mime=await _download_visual_from_message(bot,msg.reply_to_message)
        if not image_path:
            return None,None,None
        converted_path=await asyncio.to_thread(_convert_image_to_jpg,image_path)
        data_url=await asyncio.to_thread(_image_to_data_url,converted_path,"image/jpeg")
        return data_url,image_path,converted_path
    except Exception:
        for path in (image_path,converted_path):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                logger.warning("Failed to cleanup Caca vision temp after extract error | file=%s err=%r",path,e)
        raise

def _build_user_content(prompt:str,image_data_url:str|None=None):
    prompt=(prompt or "").strip()
    if not image_data_url:
        return prompt
    content=[{"type":"image_url","image_url":{"url":image_data_url}}]
    if prompt:
        content.append({"type":"text","text":prompt})
    return content

def _memory_prompt(prompt:str,has_image:bool)->str:
    prompt=(prompt or "").strip()
    if has_image and prompt:
        return f"{prompt}\n[User sent an image or static sticker.]"
    if has_image:
        return "[User sent an image or static sticker.]"
    return prompt

def _cf_credentials():
    pairs=[]
    seen=set()
    raw=[(CLOUDFLARE_ACCOUNT_ID,CLOUDFLARE_AUTH_TOKEN)]
    for i in range(2,11):
        raw.append((os.getenv(f"CLOUDFLARE_ACCOUNT_ID_{i}",""),os.getenv(f"CLOUDFLARE_AUTH_TOKEN_{i}","")))
    for account_id,token in raw:
        account_id=(account_id or "").strip()
        token=(token or "").strip()
        if not account_id or not token:
            continue
        key=(account_id,token)
        if key in seen:
            continue
        seen.add(key)
        pairs.append({"account_id":account_id,"token":token})
    return pairs

def _cf_extract_error(data,status:int)->str:
    if isinstance(data,dict):
        errors=data.get("errors")
        if isinstance(errors,list) and errors:
            msg=(errors[0] or {}).get("message")
            if msg:
                return str(msg)
        if data.get("error"):
            return str(data.get("error"))
    return f"Cloudflare HTTP {status}"

def _is_cf_quota_error(message:str)->bool:
    text=(message or "").lower()
    return (
        "daily free allocation" in text
        or "used up your daily free allocation" in text
        or "please upgrade to cloudflare's workers paid plan" in text
        or "neurons" in text
        or "quota" in text
        or "rate limit" in text
    )

def _coerce_cf_content(value):
    if isinstance(value,str):
        return value
    if isinstance(value,list):
        parts=[]
        for item in value:
            if isinstance(item,dict):
                text=item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        return "\n".join(parts).strip()
    if value:
        return str(value)
    return ""

def _extract_cf_raw(data:dict):
    result=data.get("result") or {}
    raw=result.get("response") or result.get("output_text") or result.get("text")
    if raw:
        return _coerce_cf_content(raw)
    choices=result.get("choices")
    if isinstance(choices,list) and choices:
        msg=(choices[0] or {}).get("message") or {}
        return _coerce_cf_content(msg.get("content"))
    return ""

async def _cloudflare_chat(messages:list[dict]):
    creds=_cf_credentials()
    if not creds:
        raise RuntimeError("CLOUDFLARE credentials belum diset")
    session=await get_http_session()
    errors=[]
    last_quota_error=None
    payload={
        "messages":messages,
        "temperature":0.9,
        "max_completion_tokens":1024,
        "chat_template_kwargs":{"thinking":False,"enable_thinking":False,"clear_thinking":True},
    }
    for idx,cred in enumerate(creds,start=1):
        account_id=cred["account_id"]
        token=cred["token"]
        try:
            async with session.post(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{CLOUDFLARE_MODEL}",
                headers={"Authorization":f"Bearer {token}"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=CLOUDFLARE_TIMEOUT),
            ) as r:
                data=await r.json(content_type=None)
                if r.status>=400:
                    raise RuntimeError(_cf_extract_error(data,r.status))
                if isinstance(data,dict) and data.get("success") is False:
                    raise RuntimeError(_cf_extract_error(data,r.status))
                raw=_extract_cf_raw(data)
                if not raw:
                    raise RuntimeError(f"Unexpected Cloudflare response: {data}")
                logger.info("Cloudflare success | key_index=%s account_id=%s",idx,account_id)
                return raw
        except Exception as e:
            err=str(e)
            errors.append(f"key#{idx}: {err}")
            if _is_cf_quota_error(err):
                last_quota_error=err
                logger.warning("Cloudflare quota hit | key_index=%s account_id=%s err=%s",idx,account_id,err)
                continue
            logger.warning("Cloudflare request failed | key_index=%s account_id=%s err=%s",idx,account_id,err)
            continue
    if last_quota_error and all(_is_cf_quota_error(x.split(": ",1)[-1]) for x in errors):
        raise RuntimeError("Semua API key Cloudflare terkena limit harian.")
    raise RuntimeError("Cloudflare failed: "+" | ".join(errors[-3:]))

async def meta_query(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not await require_join_or_block(update,context):
        return
    _cleanup_memory()
    msg=update.message
    if not msg or not msg.from_user:
        return
    user_id=msg.from_user.id
    chat=update.effective_chat
    em=_emo()
    if chat and chat.type in ("group","supergroup"):
        groups=await caca_db.load_groups()
        if chat.id not in groups:
            return await _reply_thread(context.bot,msg,"<b>Caca tidak tersedia di grup ini</b>",parse_mode="HTML")
    prompt=""
    use_search=False
    fresh_session=False
    image_path=None
    converted_path=None
    image_data_url=None
    stop=None
    typing=None
    try:
        if msg.text and msg.text.startswith("/caca"):
            if context.args and context.args[0].lower()=="search":
                use_search=True
                prompt=" ".join(context.args[1:]).strip()
            else:
                fresh_session=True
                prompt=" ".join(context.args).strip()
            image_data_url,image_path,converted_path=await _extract_visual_data_url(context.bot,msg)
            if not prompt and not image_data_url:
                return await _reply_thread(
                    context.bot,
                    msg,
                    f"{em} Pake gini:\n/caca <teks>\n/caca search <teks>\natau reply gambar/sticker pake /caca <pertanyaan>"
                )
        elif msg.reply_to_message:
            history=await caca_memory.get_history(user_id)
            if not history:
                return await _reply_thread(context.bot,msg,"😒 Gue ga inget ngobrol sama lu.\nKetik /caca dulu.")
            prompt=(msg.text or msg.caption or "").strip()
            image_data_url,image_path,converted_path=await _extract_visual_data_url(context.bot,msg)
        if not prompt and not image_data_url:
            return
        logger.debug("Caca typing start | chat_id=%s message_id=%s has_reply=%s",msg.chat_id,msg.message_id,bool(msg.reply_to_message))
        stop = asyncio.Event()
        thread_id = _get_thread_id(msg)
        typing = asyncio.create_task(_typing_loop(context.bot, msg.chat_id, stop, message_thread_id=thread_id))
        search_context=""
        if use_search:
            try:
                ok,results=await google_search(prompt,limit=5)
                if ok and results:
                    lines=[f"- {r['title']}\n  {r['snippet']}\n  Sumber: {r['link']}" for r in results]
                    search_context="Ini hasil search, pake buat nambah konteks, anggap ini adalah sumber terbaru. Jawab tetap sebagai Caca.\n\n"+"\n\n".join(lines)
                elif not ok:
                    logger.warning("Google Search failed | query=%r err=%r",prompt,results)
            except Exception as e:
                logger.error("Google Search unexpected error | query=%r err=%r",prompt,e,exc_info=True)
        history=[] if fresh_session else await caca_memory.get_history(user_id)
        mode=caca_db.get_mode(user_id)
        system_prompt=PERSONAS.get(mode,PERSONAS["default"])
        user_prompt=f"{search_context}\n\n{prompt}" if search_context else prompt
        messages=[{"role":"system","content":system_prompt}]+history+[{"role":"user","content":_build_user_content(user_prompt,image_data_url)}]
        raw=await _cloudflare_chat(messages)
        cleaned=_normalize_caca_output(sanitize_ai_output(_strip_thinking_leak(raw)))
        history+=[
            {"role":"user","content":_memory_prompt(prompt,bool(image_data_url))},
            {"role":"assistant","content":cleaned},
        ]
        await caca_memory.set_history(user_id,history)
        await _stop_typing_task(stop,typing)
        chunks=split_message(cleaned,4000)
        sent=None
        for chunk in chunks:
            sent=await _reply_thread(context.bot,msg,chunk,parse_mode="HTML")
        if sent:
            await caca_memory.set_last_message_id(user_id,sent.message_id)
    except Exception as e:
        await _stop_typing_task(stop,typing)
        await _reply_thread(context.bot,msg,f"{em} Error: {html.escape(str(e))}",parse_mode="HTML")
    finally:
        for path in (image_path,converted_path):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
                    logger.info("Caca vision temp deleted | file=%s",path)
            except Exception as e:
                logger.warning("Failed to delete Caca vision temp | file=%s err=%r",path,e)

def init_background():
    loop=asyncio.get_event_loop()
    loop.create_task(caca_memory.init())
    loop.create_task(caca_db.init())