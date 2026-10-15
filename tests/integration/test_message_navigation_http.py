"""Actual loopback HTTP receipt parsing with official brand selection."""
import copy

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from hermes_feishu_card.feishu_client import FeishuClient, FeishuClientConfig
from hermes_feishu_card.server import create_app, _send_card_for_app

CHAT = "oc_navigation_fixture"
MESSAGE = "om_navigation_fixture"
THREAD = "omt_navigation_fixture"
LINK = f"https://applink.feishu.cn/client/chat/open?openChatId={CHAT}&position=12"


@pytest.fixture
async def navigation_http(monkeypatch):
    receipt = {"message_id": MESSAGE, "chat_id": CHAT}
    requests = []

    async def handler(request):
        payload = await request.json()
        requests.append((request.method, request.path, payload, dict(request.query)))
        if request.path.endswith("tenant_access_token/internal"):
            return web.json_response({"code": 0, "tenant_access_token": "synthetic-token", "expire": 7200})
        if request.path == "/cardkit/v1/cards":
            return web.json_response({"code": 0, "data": {"card_id": "card_entity_fixture",
                "message_id": "om_wrong_entity", "message_app_link": LINK}})
        return web.json_response({"code": 0, "data": copy.deepcopy(receipt)})

    app = web.Application()
    app.router.add_route("POST", "/{path:.*}", handler)
    async with TestServer(app) as server:
        local = FeishuClient(FeishuClientConfig("synthetic-app", "synthetic-secret", base_url=str(server.make_url("/"))))
        branded = FeishuClient(FeishuClientConfig("synthetic-app", "synthetic-secret"))
        # Only the HTTP transport endpoint is redirected to the local server.
        # Receipt parsing retains the real configured Feishu brand, while using
        # the actual production _request_json serializer/response decoder.
        monkeypatch.setattr(branded, "_request_json", local._request_json)
        yield branded, receipt, requests


def legacy_card():
    return {"elements": [{"tag": "markdown", "content": "KEEP_BODY"}]}


@pytest.mark.asyncio
async def test_confirmed_http_send_without_navigation_remains_successful(navigation_http):
    client, receipt, requests = navigation_http
    result = await client.send_card_delivery(CHAT, legacy_card())
    assert result.message_id == MESSAGE and result.navigation is None
    assert [path for _, path, _, _ in requests] == [
        "/auth/v3/tenant_access_token/internal", "/im/v1/messages"]


@pytest.mark.asyncio
async def test_server_retains_confirmed_http_delivery_without_link(navigation_http):
    client, receipt, requests = navigation_http
    app = create_app(client)
    result = await _send_card_for_app(app, CHAT, legacy_card(), None)
    assert result.delivered and result.outcome == "delivered"
    assert result.message_id == MESSAGE and result.navigation is None
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [False, True])
async def test_http_create_and_reply_retain_only_same_response_navigation(navigation_http, reply):
    client, receipt, requests = navigation_http
    receipt.update(message_app_link=LINK, message_position="12")
    kwargs = {"reply_to_message_id": "om_anchor_fixture"} if reply else {}
    result = await client.send_card_delivery(CHAT, legacy_card(), **kwargs)
    assert result.message_id == MESSAGE and result.navigation.url == LINK
    assert result.navigation.message_id == MESSAGE and result.navigation.chat_id == CHAT
    assert [method for method, _, _, _ in requests] == ["POST", "POST"]
    assert requests[-1][1] == ("/im/v1/messages/om_anchor_fixture/reply" if reply else "/im/v1/messages")
    assert await client.send_card(CHAT, legacy_card(), **kwargs) == MESSAGE
    assert len(requests) == 3, "optional navigation must never query message history"


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [{"message_app_link": None}, {"message_app_link": {}},
    {"message_app_link": "https://[bad"}, {"message_app_link": LINK + "&position=12"},
    {"message_app_link": LINK, "message_position": True},
    {"message_app_link": LINK, "message_position": []},
    {"message_app_link": LINK, "chat_id": "oc_wrong"},
    {"message_app_link": LINK, "thread_id": THREAD}])
async def test_malformed_optional_http_metadata_cannot_change_confirmed_send(navigation_http, patch):
    client, receipt, requests = navigation_http
    receipt.update(patch)
    app = create_app(client)
    result = await _send_card_for_app(app, CHAT, legacy_card(), None)
    assert result.delivered and result.message_id == MESSAGE
    assert result.navigation is None
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [False, True])
async def test_cardkit_navigation_comes_from_final_im_receipt(navigation_http, reply):
    client, receipt, requests = navigation_http
    card = {"schema": "2.0", "config": {"streaming_mode": True}, "body": {"elements": [{"tag": "markdown", "content": "KEEP_BODY"}]}}
    kwargs = {"reply_to_message_id": "om_anchor_fixture", "thread_id": THREAD, "reply_in_thread": True} if reply else {}
    if reply:
        link = f"https://applink.feishu.cn/client/thread/open?openchatid={CHAT}&openthreadid={THREAD}&thread_position=12"
        receipt.update(message_app_link=link, thread_id=THREAD, thread_message_position="12")
    else:
        link = LINK
        receipt.update(message_app_link=link, message_position="12")
    result = await client.send_card_delivery(CHAT, card, **kwargs)
    assert result.message_id == MESSAGE and result.navigation.url == link
    assert result.navigation.message_id != "om_wrong_entity"
    assert [path for _, path, _, _ in requests] == ["/auth/v3/tenant_access_token/internal", "/cardkit/v1/cards",
        "/im/v1/messages/om_anchor_fixture/reply" if reply else "/im/v1/messages"]
    assert MESSAGE in client.cardkit.entities


@pytest.mark.asyncio
async def test_cardkit_entity_link_cannot_replace_absent_im_link(navigation_http):
    client, receipt, requests = navigation_http
    card = {"schema": "2.0", "config": {"streaming_mode": True}, "body": {"elements": [{"tag": "markdown", "content": "KEEP_BODY"}]}}
    result = await client.send_card_delivery(CHAT, card)
    assert result.message_id == MESSAGE and result.navigation is None
    assert len(requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["message", "chat", "wrong-type"])
async def test_server_discards_mismatched_optional_result_identity(damage):
    from types import SimpleNamespace
    from hermes_feishu_card.message_navigation import MessageNavigation

    navigation = MessageNavigation(MESSAGE, CHAT, LINK)
    if damage == "message":
        navigation = MessageNavigation("om_wrong", CHAT, LINK)
    elif damage == "chat":
        navigation = MessageNavigation(MESSAGE, "oc_wrong", LINK)
    else:
        navigation = {"url": LINK}

    class Client:
        async def send_card_delivery(self, chat_id, card, **kwargs):
            return SimpleNamespace(message_id=MESSAGE, retry_count=0, navigation=navigation)

    app = create_app(Client())
    result = await _send_card_for_app(app, CHAT, legacy_card(), None)
    assert result.delivered and result.message_id == MESSAGE
    assert result.navigation is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cardkit", [False, True])
@pytest.mark.parametrize("reply", [False, True])
async def test_http_position_only_receipt_generates_navigation_without_extra_requests(navigation_http, cardkit, reply):
    client, receipt, requests = navigation_http
    receipt.update(message_position="12")
    if reply:
        receipt.update(thread_id=THREAD, thread_message_position="23")
    card = {"schema": "2.0", "config": {"streaming_mode": True}, "body": {"elements": [{"tag": "markdown", "content": "KEEP_BODY"}]}} if cardkit else legacy_card()
    kwargs = {"reply_to_message_id": "om_anchor_fixture", "thread_id": THREAD,
              "reply_in_thread": True} if reply else {}
    app = create_app(client)
    result = await _send_card_for_app(app, CHAT, card, None, **kwargs)
    assert result.delivered and result.message_id == MESSAGE
    assert result.navigation.message_id == MESSAGE and result.navigation.chat_id == CHAT
    from urllib.parse import parse_qs, urlsplit
    link = urlsplit(result.navigation.url)
    query = parse_qs(link.query)
    assert link.hostname == "applink.feishu.cn"
    if reply:
        assert link.path == "/client/thread/open" and query["thread_position"] == ["23"]
        assert query["openthreadid"] == [THREAD] and query["openchatid"] == [CHAT]
    else:
        assert result.navigation.url == LINK
    paths = [path for _, path, _, _ in requests]
    assert paths == ["/auth/v3/tenant_access_token/internal"] + (["/cardkit/v1/cards"] if cardkit else []) + [
        "/im/v1/messages/om_anchor_fixture/reply" if reply else "/im/v1/messages"]
    assert all(method == "POST" for method, _, _, _ in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [
    {"message_app_link": {}}, {"message_app_link": "https://[bad"},
    {"message_position": True}, {"chat_id": "oc_wrong"},
    {"thread_id": THREAD}, {"thread_id": THREAD, "thread_message_position": "bad"}])
async def test_position_fallback_cannot_turn_bad_optional_metadata_into_send_failure(navigation_http, patch):
    client, receipt, requests = navigation_http
    receipt.update(message_position="12")
    receipt.update(patch)
    result = await client.send_card_delivery(CHAT, legacy_card())
    assert result.message_id == MESSAGE and result.navigation is None
    assert len(requests) == 2
