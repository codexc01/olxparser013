import json
import os
from pathlib import Path

import httpx
import pytest

from app.source import SourceError, fetch, key, matches, parse, url

HTML = Path(__file__).with_name('fixtures').joinpath('card.html').read_text()
URL = 'https://www.olx.uz/elektronika/q-iphone/'


def test_saved_card():
    rows, page, pages = parse(HTML)
    item, = rows
    assert (page,pages)==(0,1)
    assert item['id']=='50585795'
    assert item['currency']=='UZS' and item['price']==18807501
    assert item['city']=='Ташкент' and item['photo'].startswith('https://')
    assert item['published'].startswith('2023-10-07')


@pytest.mark.parametrize('bad',['http://olx.uz/', 'https://olx.uz.evil.test/', 'https://127.0.0.1/',
    'https://olx.uz@evil.test/', 'https://olx.uz:9000/', 'https://olx.uz//evil.test/',
    'https://www.olx.uz/d/obyavlenie/test.html','https://www.olx.uz\\@evil.test/'])
def test_unsafe_url(bad):
    with pytest.raises(ValueError):
        url(bad)


def test_key_preserves_filters():
    assert key(URL+'?b=2&a=1')==key(URL+'?a=1&b=2')
    assert key(URL+'?a=1')!=key(URL+'?a=2')
    assert key(URL+'?a=1&a=2')!=key(URL+'?a=2&a=1')


def test_structure_failure():
    with pytest.raises(SourceError):
        parse('<html>captcha</html>')


def test_missing_fields():
    ad = dict(title='Телефон',url='https://www.olx.uz/d/obyavlenie/test-IDabc.html')
    d = dict(ads=[ad],pageNumber=0,totalPages=1,totalElements=1)
    html = '<script>__PRERENDERED_STATE__='+json.dumps(json.dumps({'listing':{'listing':d}}))+'</script>'
    item = parse(html)[0][0]
    assert item['id']==item['url'] and item['price'] is None and item['photo'] is None
    assert matches(item,{}) and not matches(item,{'missing':'нет'})


def test_matching():
    a=parse(HTML)[0][0]
    assert matches(a,{'words':'IPHONE  Burgundy','mode':'все','min':100,'currency':'UZS'})
    assert not matches(a,{'currency':'USD'})
    assert not matches(a,{'exclude':'bUrgundy'})
    assert not matches(a,{'words':'iphone burgundy','mode':'фраза'})


async def test_rate_limit():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(429,headers={'Retry-After':'1500'}))) as c:
        with pytest.raises(SourceError) as exc:
            await fetch(c,URL)
        assert exc.value.delay==1500


@pytest.mark.skipif(os.getenv('LIVE_OLX')!='1', reason='Ручная проверка OLX')
async def test_real_page():
    async with httpx.AsyncClient(timeout=30,trust_env=False) as c:
        rows=await fetch(c,URL+'?page=1')
        assert rows and all(a['id'] and a['title'] for a in rows)


async def test_redirect_cannot_leave_olx():
    calls=[]
    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(302,headers={'Location':'http://127.0.0.1/private'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(SourceError):
            await fetch(client,URL)
    assert calls==[URL]


async def test_pagination_combines_pages():
    def respond(r):
        html=HTML.replace(r'\"totalPages\": 1',r'\"totalPages\": 2')
        if r.url.params.get('page')=='2':
            html=html.replace('50585795','50585796').replace(r'\"pageNumber\": 0',r'\"pageNumber\": 1')
        return httpx.Response(200,text=html)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert [a['id'] for a in await fetch(client,URL,0)]==['50585795','50585796']
