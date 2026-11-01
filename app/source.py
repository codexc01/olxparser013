import asyncio
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup


class SourceError(Exception):
    def __init__(self, message, delay=0, status=None):
        super().__init__(message)
        self.delay = delay
        self.status = status


def url(value, listing=False):
    p = urlsplit(value)
    if (len(value) > 2000 or re.search(r'[\s\\\x00-\x1f]', value)
            or p.scheme != 'https' or p.netloc.lower() not in {'olx.uz', 'www.olx.uz'}
            or not p.path.startswith('/') or p.path.startswith('//')
            or ('/obyavlenie/' in p.path) != listing):
        raise ValueError('Нужна HTTPS-ссылка на выдачу OLX.uz')
    return urlunsplit(('https', 'www.olx.uz', p.path, '' if listing else p.query, ''))


def key(value):
    p = urlsplit(url(value))
    # Stable by parameter name, preserving order of repeated values
    query = sorted(parse_qsl(p.query, keep_blank_values=True), key=lambda pair: pair[0])
    return hashlib.sha256((p.path + '?' + urlencode(query)).encode()).hexdigest()


def parse(html):
    soup = BeautifulSoup(html, 'html.parser')
    try:
        script = next(s for s in soup.find_all('script') if '__PRERENDERED_STATE__=' in (s.string or ''))
        encoded = script.string.split('__PRERENDERED_STATE__=', 1)[1].lstrip()
        state = json.loads(json.JSONDecoder().raw_decode(encoded)[0])['listing']['listing']
        ads = state['ads']
        if not isinstance(ads, list) or (not ads and state['totalElements'] != 0) or (ads and (int(state['totalPages']) < 1 or int(state['pageNumber']) >= int(state['totalPages']))):
            raise ValueError()
        rows = []
        for ad in ads:
            address = url(ad['url'], True)
            price = (ad.get('price') or {}).get('regularPrice') or {}
            loc = ad.get('location') or {}
            photo = next(iter(ad.get('photos') or []), '')
            if not re.fullmatch(r'https://[a-z0-9.-]+\.olxcdn\.com(?::443)?/[^\s]+', photo):
                photo = None
            published = ad.get('createdTime')
            if published and datetime.fromisoformat(published).tzinfo is None:
                published = None
            rows.append(dict(id=str(ad.get('id') or address), url=address, title=ad['title'],
                             price=price.get('value'), currency=price.get('currencyCode'),
                             city=loc.get('cityName'), region=loc.get('regionName'), photo=photo,
                             published=published, description=BeautifulSoup(ad.get('description') or '', 'html.parser').get_text(' ')[:5000],
                             category=ad.get('category'), attrs={p['key']: p.get('value') for p in ad.get('params') or []}))
        return rows, int(state['pageNumber']), int(state['totalPages'])
    except (StopIteration, TypeError, KeyError, ValueError, AttributeError) as exc:
        raise SourceError('Структура выдачи OLX не распознана', 900) from exc


def retry_delay(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return 0


async def fetch(client, address, gap=1.5, stats=None):
    async def request(target):
        if stats is not None:
            stats['requests'] += 1
        return await client.get(target)

    address = url(address)
    current = address
    rows = {}
    seen_pages = set()
    while True:
        r = await request(current)
        for _ in range(3):
            if not r.is_redirect:
                break
            try:
                location = url(urljoin(str(r.url), r.headers['location']))
            except (KeyError, ValueError) as exc:
                raise SourceError('Небезопасное перенаправление OLX', 900) from exc
            r = await request(location)
        if r.status_code != 200:
            delay = max(retry_delay(r.headers.get('Retry-After')), 900 if r.status_code in (403, 429) else 0)
            raise SourceError(f'HTTP {r.status_code}', delay, r.status_code)
        batch, page, total = parse(r.text)
        if page in seen_pages or len(seen_pages) >= 100 or (seen_pages and page != max(seen_pages) + 1):
            raise SourceError('Некорректная пагинация', 900)
        seen_pages.add(page)
        rows.update((a['id'], a) for a in batch)
        if page + 1 >= total or 'page' in httpx.URL(address).params:
            return list(rows.values())
        current = str(httpx.URL(address).copy_set_param('page', str(page + 2)))
        await asyncio.sleep(gap)


def norm(s):
    return ' '.join(unicodedata.normalize('NFKC', s or '').casefold().split())


def matches(ad, f):
    text = norm(ad['title'] + ' ' + (ad.get('description') or ''))
    words = norm(f.get('words'))
    if words:
        mode = f.get('mode', 'все')
        if mode == 'фраза' and words not in text:
            return False
        if mode != 'фраза' and not (any if mode == 'любое' else all)(w in text for w in words.split()):
            return False
    if any(norm(w) in text for w in f.get('exclude', '').split(',') if norm(w)):
        return False
    for k in ('city', 'region'):
        if f.get(k) and norm(f[k]) != norm(ad.get(k)):
            return False
    if f.get('condition') and norm(f['condition']) != norm(ad['attrs'].get('state')):
        return False
    if f.get('attribute'):
        k, v = f['attribute'].split('=', 1)
        if norm(ad['attrs'].get(k.strip())) != norm(v):
            return False
    if ad['price'] is None:
        return f.get('missing', 'да') == 'да'
    if f.get('currency') and ad['currency'] != f['currency']:
        return False
    return f.get('min', 0) <= ad['price'] <= f.get('max', float('inf'))
