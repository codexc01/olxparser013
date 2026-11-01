import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Update
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db import Base, Control, Match, Notice, Search, User, now
from app.monitor import cycle, deliver
from app.source import key
from app.ui import MENU, setup
from test_source import HTML, URL

pytestmark = pytest.mark.skipif(not os.getenv('TEST_DATABASE_URL'), reason='Требуется тестовая PostgreSQL')


@pytest.fixture
async def sessions():
    schema = 'test_' + uuid.uuid4().hex
    engine = create_async_engine(os.environ['TEST_DATABASE_URL'])
    async with engine.begin() as conn:
        await conn.execute(text('CREATE SCHEMA '+schema))
    test_engine = create_async_engine(os.environ['TEST_DATABASE_URL'],connect_args={'server_settings':{'search_path':schema}})
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(test_engine,expire_on_commit=False)
    async with sessions.begin() as db:
        db.add(Control(id=1))
    try:
        yield sessions
    finally:
        await test_engine.dispose()
        async with engine.begin() as conn:
            await conn.execute(text('DROP SCHEMA '+schema+' CASCADE'))
        await engine.dispose()


async def test_snapshot_restart_group_retry(sessions):
    async with sessions.begin() as db:
        db.add_all([User(id=7),User(id=8)])
        await db.flush()
        db.add_all([Search(user_id=u,name=name,url=URL,key=key(URL),interval=180)
                    for u,name in [(7,'Первый'),(7,'Второй'),(8,'Другой')]])
    sender = AsyncMock()
    sender.send_photo.return_value = SimpleNamespace(message_id=123)
    cfg = SimpleNamespace(max_age_hours=0,admin_ids=[])
    async def tick(html):
        calls=[]
        def response(r):
            calls.append(r.url)
            return httpx.Response(200,text=html)
        async with sessions.begin() as db:
            for s in await db.scalars(select(Search)):
                s.due=now()
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as c:
            await cycle(sessions,c,cfg,sender)
        assert len(calls)==1
    await tick(HTML)
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Match))==3
        assert not list(await db.scalars(select(Notice)))
    await tick(HTML.replace('50585795','50585796'))
    sender.send_photo.side_effect=TelegramRetryAfter(method=SendMessage(chat_id=7,text='x'),message='limit',retry_after=3)
    await deliver(sessions,sender)
    async with sessions.begin() as db:
        assert len(list(await db.scalars(select(Notice))))==2
        (await db.get(Control,1)).telegram_due=now()
        for n in await db.scalars(select(Notice)):
            n.due=now()
    sender.send_photo.side_effect=None
    await deliver(sessions,sender)
    await deliver(sessions,sender)
    await tick(HTML.replace('50585795','50585796'))
    await deliver(sessions,sender)
    async with sessions() as db:
        assert list(await db.scalars(select(Notice.status)))==['sent','sent']
    assert any('Первый, Второй' in call.kwargs['caption'] for call in sender.send_photo.await_args_list)


async def test_menu_and_ownership(sessions,monkeypatch):
    dp=Dispatcher()
    setup(dp)
    bot=Bot('123456:testing')
    bot.session.make_request=AsyncMock(return_value=True)
    monkeypatch.setattr('app.ui.Access.__call__', paced_access())
    seq=0
    async def feed(value, uid=15, callback=False):
        nonlocal seq
        seq+=1
        who={'id':uid,'is_bot':False,'first_name':'Тест'}
        msg={'message_id':seq,'date':1,'chat':{'id':uid,'type':'private'},'from':who,'text':value}
        data={'callback_query':{'id':str(seq),'from':who,'message':msg,'chat_instance':'a','data':value}} if callback else {'message':msg}
        await dp.feed_update(bot,Update.model_validate(dict(update_id=seq,**data)),sessions=sessions,config=SimpleNamespace(admin_ids=[]))
    await feed('/start')
    await feed(MENU[0])
    await feed('add:url',callback=True)
    for value in [URL,'Телефоны'] + ['Пропустить']*13 + ['Сохранить']:
        await feed(value)
    async with sessions() as db:
        s=await db.scalar(select(Search))
        assert s and s.url==URL and s.interval==180
        sid=s.id
    await feed(f'delete:{sid}',uid=16,callback=True)
    await feed(f'active:{sid}',callback=True)
    async with sessions() as db:
        assert not (await db.get(Search,sid)).active
    await feed(f'active:{sid}',callback=True)
    async with sessions() as db:
        assert (await db.get(Search,sid)).active
    await feed(f'delete:{sid}',callback=True)
    async with sessions() as db:
        assert await db.get(Search,sid) is None
    await bot.session.close()


def paced_access():
    from app.ui import Access
    original=Access.__call__
    async def call(self,*args):
        self.last.clear()
        return await original(self,*args)
    return call


async def test_parser_error_preserves_baseline_and_cooldown(sessions):
    bot=AsyncMock()
    async with sessions.begin() as db:
        db.add(User(id=1))
        await db.flush()
        db.add(Search(user_id=1,name='Поиск',url=URL,key=key(URL),interval=300))
    cfg=SimpleNamespace(admin_ids=[88],max_age_hours=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,text='<html>challenge</html>'))) as c:
        await cycle(sessions,c,cfg,bot)
    bot.send_message.assert_awaited_once()
    async with sessions() as db:
        s=await db.scalar(select(Search))
        assert s.initialized is None and s.success is None and s.failures==1 and s.due>now()
        assert (await db.get(Control,1)).cooldown>now()


async def test_manual_wizard_edit(sessions, monkeypatch):
    from app.ui import Access, FILTERS
    monkeypatch.setattr(Access,'__call__',paced_access())
    dp=Dispatcher()
    setup(dp)
    bot=Bot('123456:test')
    bot.session.make_request=AsyncMock(return_value=True)
    seq=0
    async def send(value,cb=False):
        nonlocal seq
        seq+=1
        who={'id':25,'is_bot':False,'first_name':'Тест'}
        msg={'message_id':seq,'date':1,'chat':{'type':'private','id':25},'from':who,'text':value}
        update={'callback_query':dict(id=str(seq),chat_instance='x',data=value,message=msg,**{'from':who})} if cb else {'message':msg}
        await dp.feed_update(bot,Update.model_validate(dict(update_id=seq,**update)),sessions=sessions,config=SimpleNamespace(admin_ids=[]))
    await send('add:manual',True)
    for value in ('Мобильные','iphone','Электроника'):
        await send(value)
    inputs={'currency':'UZS','max':'10000000','interval':'5','confirm':'Сохранить'}
    for f in FILTERS:
        await send(inputs.get(f,'Пропустить'))
    async with sessions() as db:
        s=await db.scalar(select(Search))
        assert s.url==URL and s.interval==300 and s.filters['max']==10000000
        sid=s.id
    await send(f'edit:{sid}',True)
    await send(URL+'?other=1')
    await send('Изменённый поиск')
    for f in FILTERS:
        await send('20000000' if f=='max' else inputs.get(f,'Пропустить'))
    async with sessions() as db:
        s=await db.get(Search,sid)
        assert s.filters['max']==20000000 and s.key==key(URL+'?other=1')
    await bot.session.close()


async def test_scheduler_recovers_from_database_error():
    import asyncio
    from sqlalchemy.exc import OperationalError
    from app.monitor import repeat
    resumed=asyncio.Event()
    calls=0
    async def reconnect():
        nonlocal calls
        calls+=1
        if calls==1:
            raise OperationalError('connect',{},OSError('connection lost'))
        resumed.set()
    task=asyncio.create_task(repeat(reconnect,0.01))
    try:
        await asyncio.wait_for(resumed.wait(),1)
        assert calls>=2
    finally:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
