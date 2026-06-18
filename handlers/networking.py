import html
import re
import socket
import aiohttp
import whois
import time
import ipaddress
import asyncio

from telegram import Update
from telegram.ext import ContextTypes
from utils.http import get_http_session
from urllib.parse import urlparse

_NET_CACHE = {}
_NET_CACHE_TTL = 300

#whois
def fmt_date(d):
    if isinstance(d, list):
        return str(d[0]) if d else "Not available"
    return str(d) if d else "Not available"


async def whoisdomain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text(
            "<b>📋 WHOIS Domain</b>\n\n"
            "<b>Usage:</b>\n"
            "<code>/whoisdomain google.com</code>",
            parse_mode="HTML"
        )

    domain = (
        context.args[0]
        .replace("http://", "")
        .replace("https://", "")
        .split("/")[0]
    )

    if not re.match(r"^[\w.-]+$", domain) or domain.startswith("-"):
        return await update.message.reply_text(
            "❌ <b>Invalid domain format</b>",
            parse_mode="HTML"
        )

    msg = await update.message.reply_text(
        f"🔄 <b>Fetching WHOIS for {html.escape(domain)}...</b>",
        parse_mode="HTML"
    )

    try:
        w = await asyncio.to_thread(whois.whois, domain)

        ns = w.name_servers
        if isinstance(ns, list):
            ns_text = "\n".join(f"• {html.escape(n)}" for n in ns[:8])
        else:
            ns_text = html.escape(str(ns)) if ns else "Not available"

        result = (
            "<b>📋 WHOIS Information</b>\n\n"
            f"<b>Domain:</b> <code>{html.escape(domain)}</code>\n"
            f"<b>Registrar:</b> {html.escape(str(w.registrar or 'N/A'))}\n"
            f"<b>WHOIS Server:</b> {html.escape(str(w.whois_server or 'N/A'))}\n\n"

            "<b>📅 Important Dates</b>\n"
            f"<b>Created:</b> {fmt_date(w.creation_date)}\n"
            f"<b>Updated:</b> {fmt_date(w.updated_date)}\n"
            f"<b>Expires:</b> {fmt_date(w.expiration_date)}\n\n"

            "<b>👤 Registrant</b>\n"
            f"<b>Name:</b> {html.escape(str(w.name or 'N/A'))}\n"
            f"<b>Organization:</b> {html.escape(str(w.org or 'N/A'))}\n"
            f"<b>Email:</b> {html.escape(str(w.emails[0] if isinstance(w.emails, list) else w.emails or 'N/A'))}\n\n"

            "<b>🔧 Technical</b>\n"
            f"<b>Status:</b> {html.escape(str(w.status or 'N/A'))}\n"
            f"<b>DNSSEC:</b> {html.escape(str(w.dnssec or 'N/A'))}\n\n"

            "<b>🌐 Name Servers</b>\n"
            f"{ns_text}\n\n"

            "<b>🏢 Registrar Info</b>\n"
            f"<b>IANA ID:</b> {html.escape(str(w.registrar_iana_id or 'N/A'))}\n"
            f"<b>URL:</b> {html.escape(str(w.registrar_url or 'N/A'))}"
        )

        if len(result) > 4096:
            await msg.edit_text(result[:4096], parse_mode="HTML")
            await update.message.reply_text(result[4096:], parse_mode="HTML")
        else:
            await msg.edit_text(result, parse_mode="HTML")

    except Exception as e:
        await msg.edit_text(
            f"❌ WHOIS failed: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML"
        )
        
#cmd ip
async def ip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text(
            "<b>🌍 IP Info</b>\n\n"
            "<b>Usage:</b>\n"
            "<code>/ip 8.8.8.8</code>",
            parse_mode="HTML"
        )

    target = context.args[0].strip()
    try:
        ip_obj = ipaddress.ip_address(target)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
            return await update.message.reply_text("❌ Private/local IP addresses are not allowed.")
        ip = str(ip_obj)
    except ValueError:
        try:
            resolved_ip_str = await asyncio.to_thread(socket.gethostbyname, target)
            ip_obj = ipaddress.ip_address(resolved_ip_str)
            if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
                return await update.message.reply_text("❌ Private/local IP addresses are not allowed.")
            ip = target
        except Exception:
            return await update.message.reply_text("❌ Invalid IP address or domain name.")

    msg = await update.message.reply_text(
        f"🔄 <b>Analyzing IP {html.escape(ip)}...</b>",
        parse_mode="HTML"
    )

    try:
        url = (
            f"http://ip-api.com/json/{ip}"
            "?fields=status,message,continent,continentCode,country,countryCode,"
            "region,regionName,city,zip,lat,lon,timezone,offset,isp,org,as,"
            "reverse,mobile,proxy,hosting,query"
        )

        session = await get_http_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return await msg.edit_text("❌ Failed to fetch IP information")

            data = await resp.json()

        if data.get("status") != "success":
            return await msg.edit_text(
                f"❌ Failed: <code>{html.escape(data.get('message', 'Unknown error'))}</code>",
                parse_mode="HTML"
            )

        text = (
            "<b>🌍 IP Address Information</b>\n\n"
            f"<b>IP:</b> <code>{data.get('query')}</code>\n"
            f"<b>ISP:</b> {html.escape(data.get('isp','N/A'))}\n"
            f"<b>Organization:</b> {html.escape(data.get('org','N/A'))}\n"
            f"<b>AS:</b> {html.escape(data.get('as','N/A'))}\n\n"

            "<b>📍 Location</b>\n"
            f"<b>Country:</b> {html.escape(data.get('country','N/A'))} ({data.get('countryCode','')})\n"
            f"<b>Region:</b> {html.escape(data.get('regionName','N/A'))}\n"
            f"<b>City:</b> {html.escape(data.get('city','N/A'))}\n"
            f"<b>ZIP:</b> {html.escape(data.get('zip','N/A'))}\n"
            f"<b>Coords:</b> {data.get('lat','N/A')}, {data.get('lon','N/A')}\n\n"

            "<b>🕐 Timezone</b>\n"
            f"<b>TZ:</b> {html.escape(data.get('timezone','N/A'))}\n"
            f"<b>UTC Offset:</b> {data.get('offset','N/A')}\n\n"

            "<b>🔍 Flags</b>\n"
            f"<b>Reverse DNS:</b> {html.escape(data.get('reverse','N/A'))}\n"
            f"<b>Mobile:</b> {'Yes' if data.get('mobile') else 'No'}\n"
            f"<b>Proxy:</b> {'Yes' if data.get('proxy') else 'No'}\n"
            f"<b>Hosting:</b> {'Yes' if data.get('hosting') else 'No'}"
        )

        await msg.edit_text(text, parse_mode="HTML")

    except Exception as e:
        await msg.edit_text(
            f"❌ Error: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML"
        )
        

async def domain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /domain example.com
    """
    msg = update.effective_message

    if not context.args:
        return await msg.reply_text(
            "<b>Usage:</b> /domain &lt;domain&gt;\n"
            "<b>Example:</b> /domain google.com",
            parse_mode="HTML"
        )

    domain = context.args[0]
    domain = domain.replace("http://", "").replace("https://", "").split("/")[0]

    if not re.match(r"^[\w.-]+$", domain) or domain.startswith("-"):
        return await msg.reply_text(
            "❌ <b>Invalid domain format</b>",
            parse_mode="HTML"
        )

    loading = await msg.reply_text(
        f"🔄 <b>Analyzing domain:</b> <code>{html.escape(domain)}</code>",
        parse_mode="HTML"
    )

    info = {}

    try:
        info["ip"] = await asyncio.to_thread(socket.gethostbyname, domain)
    except Exception:
        info["ip"] = "Not found"

    try:
        w = await asyncio.to_thread(whois.whois, domain)
        info["registrar"] = w.registrar or "Not available"
        info["created"] = str(w.creation_date) if w.creation_date else "Not available"
        info["expires"] = str(w.expiration_date) if w.expiration_date else "Not available"
        info["nameservers"] = w.name_servers or []
    except Exception:
        info["registrar"] = "Not available"
        info["created"] = "Not available"
        info["expires"] = "Not available"
        info["nameservers"] = []

    is_safe = False
    if info["ip"] != "Not found":
        try:
            ip = ipaddress.ip_address(info["ip"])
            if not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast):
                is_safe = True
        except ValueError:
            pass

    if not is_safe:
        if info["ip"] == "Not found":
            info["http_status"] = "Not available"
            info["server"] = "Not available"
        else:
            info["http_status"] = "Blocked (Unsafe IP)"
            info["server"] = "N/A"
    else:
        try:
            session = await get_http_session()
            async with session.get(
                f"http://{info['ip']}",
                headers={"Host": domain},
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=False
            ) as r:
                info["http_status"] = r.status
                info["server"] = r.headers.get("server", "Not available")
        except Exception:
            info["http_status"] = "Not available"
            info["server"] = "Not available"

    if info["nameservers"]:
        ns_text = "\n".join(
            f"• {html.escape(ns)}" for ns in info["nameservers"][:5]
        )
    else:
        ns_text = "Not available"

    text = (
        "<b>🌐 Domain Information</b>\n\n"
        f"<b>Domain:</b> <code>{html.escape(domain)}</code>\n"
        f"<b>IP Address:</b> <code>{info['ip']}</code>\n"
        f"<b>HTTP Status:</b> <code>{info['http_status']}</code>\n"
        f"<b>Server:</b> <code>{html.escape(info['server'])}</code>\n\n"
        "<b>📋 Registration Details</b>\n"
        f"<b>Registrar:</b> {html.escape(info['registrar'])}\n"
        f"<b>Created:</b> {html.escape(info['created'])}\n"
        f"<b>Expires:</b> {html.escape(info['expires'])}\n\n"
        "<b>🔧 Name Servers</b>\n"
        f"{ns_text}"
    )

    await loading.edit_text(text, parse_mode="HTML")
    
def _cache_cleanup():
    now = time.time()
    expired = [k for k, v in list(_NET_CACHE.items()) if now - v[0] > _NET_CACHE_TTL]
    for k in expired:
        _NET_CACHE.pop(k, None)

def _cache_get(key: str):
    _cache_cleanup()
    item = _NET_CACHE.get(key)
    if not item:
        return None
    ts, val = item
    if time.time() - ts > _NET_CACHE_TTL:
        _NET_CACHE.pop(key, None)
        return None
    return val

def _cache_set(key: str, val):
    _cache_cleanup()
    _NET_CACHE[key] = (time.time(), val)


def _fmt_date(d):
    if isinstance(d, list):
        return str(d[0]) if d else "Not available"
    return str(d) if d else "Not available"


def _split_tg(text: str, limit: int = 4096):
    parts = []
    cur = text
    while len(cur) > limit:
        cut = cur.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(cur[:cut])
        cur = cur[cut:].lstrip("\n")
    if cur:
        parts.append(cur)
    return parts


def _is_ip(s: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET, s)
        return True
    except Exception:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, s)
        return True
    except Exception:
        return False


def _normalize_input(raw: str) -> str:
    t = (raw or "").strip().replace("\u200b", "")
    t = t.split("\n")[0].strip()
    return t


def _extract_host_port(raw: str):
    raw = _normalize_input(raw)
    if not raw:
        return None, None, None

    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        u = urlparse(raw)
        host = u.hostname
        port = u.port
        return raw, host, port

    if raw.startswith("//"):
        u = urlparse("http:" + raw)
        host = u.hostname
        port = u.port
        return "http:" + raw, host, port

    if "/" in raw:
        u = urlparse("http://" + raw)
        host = u.hostname
        port = u.port
        return "http://" + raw, host, port

    host = raw
    port = None

    if host.count(":") == 1 and not host.startswith("["):
        h, p = host.split(":", 1)
        if p.isdigit():
            host = h
            port = int(p)

    if host.startswith("[") and "]" in host:
        h = host[1:host.index("]")]
        rest = host[host.index("]") + 1:]
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
        host = h

    return None, host, port


async def _resolve_ips(host: str):
    ips_v4 = []
    ips_v6 = []
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None)
        for fam, _, _, _, sockaddr in infos:
            if fam == socket.AF_INET:
                ip = sockaddr[0]
                if ip not in ips_v4:
                    ips_v4.append(ip)
            elif fam == socket.AF_INET6:
                ip = sockaddr[0]
                if ip not in ips_v6:
                    ips_v6.append(ip)
    except Exception:
        pass
    return ips_v4, ips_v6


async def _reverse_ptr(ip: str):
    try:
        loop = asyncio.get_running_loop()
        host, _ = await loop.getnameinfo((ip, 0), socket.NI_NAMEREQD)
        return host
    except Exception:
        return None


async def _fetch_ip_info(ip: str):
    cache_key = f"ip:{ip}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = (
        f"http://ip-api.com/json/{ip}"
        "?fields=status,message,continent,continentCode,country,countryCode,"
        "region,regionName,city,zip,lat,lon,timezone,offset,isp,org,as,"
        "reverse,mobile,proxy,hosting,query"
    )

    session = await get_http_session()
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status != 200:
            _cache_set(cache_key, None)
            return None
        data = await resp.json()

    if data.get("status") != "success":
        _cache_set(cache_key, {"error": data.get("message") or "Unknown error"})
        return {"error": data.get("message") or "Unknown error"}

    _cache_set(cache_key, data)
    return data


async def _fetch_http_fingerprint(host: str, port: int | None):
    cache_key = f"httpfp:{host}:{port or ''}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    session = await get_http_session()

    async def probe(url: str):
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=12),
                allow_redirects=True
            ) as r:
                headers = {k.lower(): v for k, v in r.headers.items()}
                return {
                    "ok": True,
                    "url": str(r.url),
                    "status": int(r.status),
                    "server": headers.get("server"),
                    "content_type": headers.get("content-type"),
                    "hsts": headers.get("strict-transport-security"),
                    "cf_ray": headers.get("cf-ray"),
                    "via": headers.get("via"),
                }
        except Exception as e:
            return {"ok": False, "err": str(e)}

    if port:
        https_url = f"https://{host}:{port}/"
        http_url = f"http://{host}:{port}/"
    else:
        https_url = f"https://{host}/"
        http_url = f"http://{host}/"

    r1 = await probe(https_url)
    r2 = None
    if not r1.get("ok"):
        r2 = await probe(http_url)

    out = {"https": r1, "http": r2}
    _cache_set(cache_key, out)
    return out


async def _fetch_whois_domain(domain: str):
    cache_key = f"whois:{domain}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        w = await asyncio.to_thread(whois.whois, domain)
        _cache_set(cache_key, w)
        return w
    except Exception as e:
        _cache_set(cache_key, {"error": str(e)})
        return {"error": str(e)}


def _fmt_bool(x):
    return "Yes" if x else "No"


async def net_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    if not context.args:
        text = (
            "<b>NET</b>\n\n"
            "<b>Usage:</b>\n"
            "• <code>/net 8.8.8.8</code>\n"
            "• <code>/net google.com</code>\n"
            "• <code>/net https://example.com/path</code>\n"
            "• <code>/net 1.1.1.1:443</code>\n"
        )
        return await msg.reply_text(text, parse_mode="HTML")

    raw = " ".join(context.args).strip()
    _, host, port = _extract_host_port(raw)
    if not host:
        return await msg.reply_text("Input invalid.", parse_mode="HTML")

    loading = await msg.reply_text(
        f"<b>Analyzing:</b> <code>{html.escape(host)}</code>",
        parse_mode="HTML"
    )

    target_is_ip = _is_ip(host)
    ips_v4, ips_v6 = ([], [])
    ptr = None

    if target_is_ip:
        ptr = await _reverse_ptr(host)
        ip_for_geo = host
    else:
        ips_v4, ips_v6 = await _resolve_ips(host)
        ip_for_geo = (ips_v4[0] if ips_v4 else (ips_v6[0] if ips_v6 else None))

    ip_info = await _fetch_ip_info(ip_for_geo) if ip_for_geo else None
    httpfp = await _fetch_http_fingerprint(host, port) if not target_is_ip else None
    w = await _fetch_whois_domain(host) if not target_is_ip else None

    lines = []
    lines.append("<b>NET Report</b>\n")
    lines.append(f"<b>Input:</b> <code>{html.escape(raw)}</code>")
    lines.append(f"<b>Host:</b> <code>{html.escape(host)}</code>")
    if port:
        lines.append(f"<b>Port:</b> <code>{port}</code>")

    if target_is_ip:
        lines.append(f"<b>Type:</b> <code>IP</code>")
        if ptr:
            lines.append(f"<b>PTR:</b> <code>{html.escape(ptr)}</code>")
    else:
        lines.append(f"<b>Type:</b> <code>Domain</code>")
        if ips_v4:
            lines.append(f"<b>A:</b> <code>{html.escape(', '.join(ips_v4[:6]))}</code>")
        else:
            lines.append("<b>A:</b> <code>Not found</code>")
        if ips_v6:
            lines.append(f"<b>AAAA:</b> <code>{html.escape(', '.join(ips_v6[:6]))}</code>")
        else:
            lines.append("<b>AAAA:</b> <code>Not found</code>")

    lines.append("")

    if ip_for_geo:
        lines.append("<b>🌍 IP / ASN</b>")
        if isinstance(ip_info, dict) and ip_info.get("error"):
            lines.append(f"<b>IP API:</b> <code>{html.escape(str(ip_info.get('error')))}</code>")
        elif isinstance(ip_info, dict) and ip_info.get("status") == "success":
            lines.append(f"<b>IP:</b> <code>{html.escape(str(ip_info.get('query')))}</code>")
            lines.append(f"<b>ISP:</b> {html.escape(ip_info.get('isp','N/A'))}")
            lines.append(f"<b>Org:</b> {html.escape(ip_info.get('org','N/A'))}")
            lines.append(f"<b>AS:</b> {html.escape(ip_info.get('as','N/A'))}")
            lines.append(
                f"<b>Location:</b> {html.escape(ip_info.get('country','N/A'))} "
                f"({html.escape(ip_info.get('countryCode',''))}) / "
                f"{html.escape(ip_info.get('regionName','N/A'))} / "
                f"{html.escape(ip_info.get('city','N/A'))}"
            )
            lines.append(f"<b>Timezone:</b> {html.escape(ip_info.get('timezone','N/A'))} (UTC {ip_info.get('offset','N/A')})")
            rev = ip_info.get("reverse")
            if rev:
                lines.append(f"<b>Reverse:</b> <code>{html.escape(str(rev))}</code>")
            lines.append(
                "<b>Flags:</b> "
                f"Mobile={_fmt_bool(ip_info.get('mobile'))}, "
                f"Proxy={_fmt_bool(ip_info.get('proxy'))}, "
                f"Hosting={_fmt_bool(ip_info.get('hosting'))}"
            )
        else:
            lines.append("<code>Not available</code>")
    else:
        lines.append("<b>🌍 IP / ASN</b>")
        lines.append("<code>Not available</code>")

    lines.append("")

    if httpfp:
        lines.append("<b>HTTP Fingerprint</b>")
        https_r = httpfp.get("https") or {}
        http_r = httpfp.get("http") or {}

        if https_r.get("ok"):
            lines.append("<b>HTTPS:</b> <b>OK</b>")
            lines.append(f"<b>Status:</b> <code>{https_r.get('status')}</code>")
            lines.append(f"<b>Final URL:</b> <code>{html.escape(https_r.get('url',''))}</code>")
            if https_r.get("server"):
                lines.append(f"<b>Server:</b> <code>{html.escape(https_r.get('server'))}</code>")
            if https_r.get("content_type"):
                lines.append(f"<b>Content-Type:</b> <code>{html.escape(https_r.get('content_type'))}</code>")
            lines.append(f"<b>HSTS:</b> <code>{'Yes' if https_r.get('hsts') else 'No'}</code>")
        else:
            lines.append("<b>HTTPS:</b>")
            if https_r.get("err"):
                lines.append(f"<code>{html.escape(https_r.get('err'))}</code>")

        if http_r:
            if http_r.get("ok"):
                lines.append("")
                lines.append("<b>HTTP:</b> <b>OK</b>")
                lines.append(f"<b>Status:</b> <code>{http_r.get('status')}</code>")
                lines.append(f"<b>Final URL:</b> <code>{html.escape(http_r.get('url',''))}</code>")
                if http_r.get("server"):
                    lines.append(f"<b>Server:</b> <code>{html.escape(http_r.get('server'))}</code>")
                if http_r.get("content_type"):
                    lines.append(f"<b>Content-Type:</b> <code>{html.escape(http_r.get('content_type'))}</code>")
            else:
                lines.append("")
                lines.append("<b>HTTP:</b>")
                if http_r.get("err"):
                    lines.append(f"<code>{html.escape(http_r.get('err'))}</code>")

        cf_hint = None
        if (https_r.get("cf_ray") or "").strip():
            cf_hint = "Cloudflare"
        elif (https_r.get("server") or "").lower() == "cloudflare":
            cf_hint = "Cloudflare"
        elif (http_r.get("server") or "").lower() == "cloudflare":
            cf_hint = "Cloudflare"

        if cf_hint:
            lines.append("")
            lines.append(f"<b>CDN/WAF:</b> <code>{cf_hint}</code>")

    if w and isinstance(w, dict) and w.get("error"):
        lines.append("")
        lines.append("<b>📋 WHOIS</b>")
        lines.append(f"<code>{html.escape(str(w.get('error')))}</code>")

    elif w and not isinstance(w, dict):
        ns = getattr(w, "name_servers", None)
        if isinstance(ns, list):
            ns_text = "\n".join(f"• {html.escape(str(n))}" for n in ns[:8])
        else:
            ns_text = html.escape(str(ns)) if ns else "Not available"

        email_val = getattr(w, "emails", None)
        if isinstance(email_val, list):
            email_val = email_val[0] if email_val else None

        lines.append("")
        lines.append("<b>📋 WHOIS</b>")
        lines.append(f"<b>Registrar:</b> {html.escape(str(getattr(w, 'registrar', None) or 'N/A'))}")
        lines.append(f"<b>WHOIS Server:</b> {html.escape(str(getattr(w, 'whois_server', None) or 'N/A'))}")
        lines.append(f"<b>Created:</b> {_fmt_date(getattr(w, 'creation_date', None))}")
        lines.append(f"<b>Updated:</b> {_fmt_date(getattr(w, 'updated_date', None))}")
        lines.append(f"<b>Expires:</b> {_fmt_date(getattr(w, 'expiration_date', None))}")
        lines.append(f"<b>Registrant:</b> {html.escape(str(getattr(w, 'name', None) or 'N/A'))}")
        lines.append(f"<b>Org:</b> {html.escape(str(getattr(w, 'org', None) or 'N/A'))}")
        lines.append(f"<b>Email:</b> {html.escape(str(email_val or 'N/A'))}")
        lines.append("<b>Name Servers:</b>")
        lines.append(ns_text)

    out = "\n".join(lines).strip()

    parts = _split_tg(out, 4096)
    try:
        await loading.edit_text(parts[0], parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        await msg.reply_text(parts[0], parse_mode="HTML", disable_web_page_preview=True)

    for p in parts[1:]:
        await msg.reply_text(p, parse_mode="HTML", disable_web_page_preview=True)
        