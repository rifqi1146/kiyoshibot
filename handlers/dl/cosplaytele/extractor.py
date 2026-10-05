"""FLOW SCRAPER
1. Fetch the post with curl_cffi; parse title, gallery photos, and Cossora video embed.
2. Preserve gallery order, deduplicate URLs, exclude sidebar recommendations.
3. Detect Cossora video embed (`cossora.stream/embed/<uuid>`) if present.
4. Return {title, images, video_embed} — video resolution left blank for the
   worker to decide (single HLS stream, no picker).
"""
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests

from .constants import UA, _HTTP_TIMEOUT, _IMAGE_EXT
from .cossora import find_embed


def is_cosplaytele_url(url):
    try:
        p = urlsplit(url)
        parts = p.path.strip('/').split('/')
        return (p.scheme in ('https', 'http')
                and p.hostname in ('cosplaytele.com', 'www.cosplaytele.com')
                and not p.username and not p.password and p.port in (None, 80, 443)
                and len(parts) == 1 and bool(parts[0])
                and '@' not in parts[0]
                and parts[0] not in ('category', 'tag', 'page', 'feed', 'about', 'contact', 'wp-admin')
                and not p.query)
    except ValueError:
        return False


def parse_post(text, url):
    soup = BeautifulSoup(text, 'html.parser')
    heading = soup.select_one('h1.entry-title, h1')
    title = heading.get_text(' ', strip=True) if heading else 'Cosplaytele'
    images = []
    for img in soup.select('.gallery .gallery-icon img, .wp-block-gallery img'):
        src = img.get('data-src') or img.get('src') or ''
        anchor = img.find_parent('a')
        if anchor and urlsplit(anchor.get('href', '')).path.lower().endswith(_IMAGE_EXT):
            src = anchor['href']
        target = urljoin(url, src)
        p = urlsplit(target)
        if (p.scheme == 'https' and p.hostname in ('cosplaytele.com', 'www.cosplaytele.com')
                and not p.username and not p.password and p.port in (None, 443)
                and p.path.startswith('/wp-content/uploads/')
                and p.path.lower().endswith(_IMAGE_EXT)):
            images.append(target)
    images = list(dict.fromkeys(images))
    video_embed = find_embed(text)
    if not images and not video_embed:
        raise RuntimeError('No supported photo gallery or video found in this Cosplaytele post.')
    return {'title': title, 'images': images, 'video_embed': video_embed}


def scrape_post(url):
    if not is_cosplaytele_url(url):
        raise ValueError('Invalid Cosplaytele post URL.')
    with requests.Session(impersonate='chrome') as session:
        response = session.get(url, headers={'User-Agent': UA}, timeout=_HTTP_TIMEOUT,
                               allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f'Cosplaytele HTTP {response.status_code}')
        return parse_post(response.content.decode('utf-8', 'replace'), url)
