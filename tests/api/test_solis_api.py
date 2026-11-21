import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast
from unittest.mock import DEFAULT, AsyncMock, patch

import pytest
from aiohttp import ClientConnectionError, ClientSession, web
from aiohttp.test_utils import TestClient

from custom_components.solis_cloud_control.api.solis_api import SolisCloudControlApiClient, SolisCloudControlApiError
from custom_components.solis_cloud_control.utils.retry_policy import RetryPolicy

if TYPE_CHECKING:
    from pytest_aiohttp.plugin import AiohttpClient

type ApiClientFactory = Callable[[TestClient[web.Request, web.Application]], SolisCloudControlApiClient]
type MockEndpoint = Callable[[web.Request], Awaitable[web.Response]]


@pytest.fixture
def create_api_client() -> ApiClientFactory:
    def _create_api_client(client: TestClient[web.Request, web.Application]) -> SolisCloudControlApiClient:
        return SolisCloudControlApiClient(
            base_url="",
            api_key="any key",
            api_token="any token",
            # TestClient proxies requests to its session, which is all the API client needs
            session=cast("ClientSession", client),
        )

    return _create_api_client


async def test_read(create_api_client: ApiClientFactory, aiohttp_client: AiohttpClient) -> None:
    any_inverter_sn = "any inverter sn"
    any_cid = -1
    any_result = "any result"

    async def mock_read_endpoint(request: web.Request) -> web.Response:
        body = await request.json()
        assert body.get("inverterSn") == any_inverter_sn
        assert body.get("cid") == any_cid
        return web.json_response({"code": "0", "msg": "Success", "data": {"msg": any_result}})

    app = web.Application()
    app.router.add_route("POST", SolisCloudControlApiClient._READ_ENDPOINT, mock_read_endpoint)

    client = await aiohttp_client(app)
    api_client = create_api_client(client)

    result = await api_client.read(inverter_sn=any_inverter_sn, cid=any_cid, max_retry_time=0)

    assert result == any_result


async def test_read_batch(create_api_client: ApiClientFactory, aiohttp_client: AiohttpClient) -> None:
    any_inverter_sn = "any inverter sn"
    any_cids = [-1, -2, -3]
    any_results = {-1: "any result 1", -2: "any result 2", -3: "any result 3"}

    async def mock_read_batch_endpoint(request: web.Request) -> web.Response:
        body = await request.json()
        assert body.get("inverterSn") == any_inverter_sn
        assert body.get("cids") == ",".join(map(str, any_cids))
        data = [[{"cid": cid, "msg": msg} for cid, msg in any_results.items()]]
        return web.json_response({"code": "0", "msg": "Success", "data": data})

    app = web.Application()
    app.router.add_route("POST", SolisCloudControlApiClient._READ_BATCH_ENDPOINT, mock_read_batch_endpoint)

    client = await aiohttp_client(app)
    api_client = create_api_client(client)

    result = await api_client.read_batch(inverter_sn=any_inverter_sn, cids=any_cids, max_retry_time=0)

    assert result == any_results


async def test_control(create_api_client: ApiClientFactory, aiohttp_client: AiohttpClient) -> None:
    any_inverter_sn = "any inverter sn"
    any_cid = -1
    any_value = "any value"
    any_old_value = "any old value"

    async def mock_control_endpoint(request: web.Request) -> web.Response:
        body = await request.json()
        assert body.get("inverterSn") == any_inverter_sn
        assert body.get("cid") == any_cid
        assert body.get("value") == any_value
        assert body.get("yuanzhi") == any_old_value
        return web.json_response({"code": "0", "msg": "Success", "data": [{"code": "0", "msg": "Success"}]})

    app = web.Application()
    app.router.add_route("POST", SolisCloudControlApiClient._CONTROL_ENDPOINT, mock_control_endpoint)

    client = await aiohttp_client(app)
    api_client = create_api_client(client)

    await api_client.control(inverter_sn=any_inverter_sn, cid=any_cid, value=any_value, old_value=any_old_value)


async def test_inverter_list(create_api_client: ApiClientFactory, aiohttp_client: AiohttpClient) -> None:
    any_records = [
        {"field 1": "record 1, value 1", "field2": "record 1, value 2"},
        {"field 1": "record 2, value 1", "field 2": "record 2, value 2"},
    ]

    async def mock_inverter_list_endpoint(request: web.Request) -> web.Response:
        body = await request.json()
        assert body.get("pageSize") == "100"
        return web.json_response({"code": "0", "msg": "Success", "data": {"page": {"records": any_records}}})

    app = web.Application()
    app.router.add_route("POST", SolisCloudControlApiClient._INVERTER_LIST_ENDPOINT, mock_inverter_list_endpoint)

    client = await aiohttp_client(app)
    api_client = create_api_client(client)

    result = await api_client.inverter_list(max_retry_time=0)

    assert result == any_records


async def test_inverter_details(create_api_client: ApiClientFactory, aiohttp_client: AiohttpClient) -> None:
    any_inverter_sn = "any_inverter_sn"
    any_details_response = {"field1": "value1", "field2": "value2"}

    async def mock_inverter_details_endpoint(request: web.Request) -> web.Response:
        body = await request.json()
        assert body.get("sn") == any_inverter_sn
        return web.json_response({"code": "0", "msg": "Success", "data": any_details_response})

    app = web.Application()
    app.router.add_route("POST", SolisCloudControlApiClient._INVERTER_DETAILS_ENDPOINT, mock_inverter_details_endpoint)

    client = await aiohttp_client(app)
    api_client = create_api_client(client)

    result = await api_client.inverter_details(inverter_sn=any_inverter_sn, max_retry_time=0)

    assert result == any_details_response


async def mock_read_endpoint_http_error(request: web.Request) -> web.Response:
    return web.Response(status=500, text="Internal Server Error")


async def mock_read_endpoint_api_error(request: web.Request) -> web.Response:
    return web.json_response({"code": "100", "msg": "API Error"})


async def mock_read_endpoint_missing_data_field(request: web.Request) -> web.Response:
    return web.json_response({"code": "0", "msg": "Success"})


async def mock_read_endpoint_missing_msg_field(request: web.Request) -> web.Response:
    return web.json_response({"code": "0", "msg": "Success", "data": {"unknown field": "any value"}})


@pytest.mark.parametrize(
    "mock_endpoint,expected_error",
    [
        (mock_read_endpoint_http_error, SolisCloudControlApiError("Internal Server Error", status_code=500)),
        (
            mock_read_endpoint_api_error,
            SolisCloudControlApiError("API operation failed: API Error", response_code="100"),
        ),
        (
            mock_read_endpoint_missing_data_field,
            SolisCloudControlApiError("Read failed: missing 'data' field"),
        ),
        (
            mock_read_endpoint_missing_msg_field,
            SolisCloudControlApiError("Read failed: missing 'msg' field"),
        ),
    ],
)
async def test_read_errors(
    mock_endpoint: MockEndpoint,
    expected_error: SolisCloudControlApiError,
    create_api_client: ApiClientFactory,
    aiohttp_client: AiohttpClient,
) -> None:
    app = web.Application()
    app.router.add_route("POST", SolisCloudControlApiClient._READ_ENDPOINT, mock_endpoint)

    client = await aiohttp_client(app)
    api_client = create_api_client(client)

    with pytest.raises(SolisCloudControlApiError) as excinfo:
        await api_client.read(inverter_sn="any inverter", cid=-1, max_retry_time=0)

    assert str(excinfo.value) == str(expected_error)


@pytest.mark.parametrize(("status", "code"), [(200, "B0072"), (200, "Z0001"), (401, None), (400, None)])
async def test_read_does_not_retry_expected_or_permanent_errors(
    create_api_client: ApiClientFactory,
    aiohttp_client: AiohttpClient,
    caplog: pytest.LogCaptureFixture,
    status: int,
    code: str | None,
) -> None:
    requests = 0

    async def endpoint(_request: web.Request) -> web.Response:
        nonlocal requests
        requests += 1
        return web.json_response({"code": code, "msg": "API error"}, status=status)

    app = web.Application()
    app.router.add_post(SolisCloudControlApiClient._READ_ENDPOINT, endpoint)
    api_client = create_api_client(await aiohttp_client(app))

    with pytest.raises(SolisCloudControlApiError):
        await api_client.read("sn", 15)

    assert requests == 1
    assert not any(record.levelno >= logging.WARNING and "Retrying" in record.message for record in caplog.records)


@pytest.mark.parametrize(
    ("status", "retry_after", "expected_delay"),
    [
        (429, "4", 4),
        (429, None, 30),
        (429, "invalid", 30),
        (429, "Sat, 21 Nov 2020 14:00:00 GMT", 1),
        (429, "Sat, 21 Nov 2020 14:00:00", 1),
        (429, "9" * 400, 30),
        (429, "120", None),
        (503, None, 1),
    ],
)
async def test_read_backoff(
    create_api_client: ApiClientFactory,
    aiohttp_client: AiohttpClient,
    status: int,
    retry_after: str | None,
    expected_delay: float | None,
) -> None:
    requests = 0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async def endpoint(_request: web.Request) -> web.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            headers = {"Retry-After": retry_after} if retry_after is not None else None
            return web.Response(status=status, headers=headers, text="Temporarily unavailable")
        return web.json_response({"code": "0", "data": {"msg": "55"}})

    app = web.Application()
    app.router.add_post(SolisCloudControlApiClient._READ_ENDPOINT, endpoint)
    api_client = create_api_client(await aiohttp_client(app))
    retry_policy = RetryPolicy(retryable_exception=SolisCloudControlApiError, sleep=sleep)

    with patch.object(api_client, "_retry_policy", retry_policy):
        if expected_delay is None:
            with pytest.raises(SolisCloudControlApiError) as error:
                await api_client.read("sn", 15)
            assert error.value.retry_after == 120
            assert requests == 1
            assert delays == []
        else:
            assert await api_client.read("sn", 15) == "55"
            assert requests == 2
            assert delays == [expected_delay]


async def test_read_deadline_cancels_request(
    create_api_client: ApiClientFactory, aiohttp_client: AiohttpClient
) -> None:
    cancelled = asyncio.Event()

    async def slow_request(*_args: object) -> None:
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    api_client = create_api_client(await aiohttp_client(web.Application()))
    with (
        patch.object(api_client, "_execute_request", side_effect=slow_request),
        pytest.raises(SolisCloudControlApiError, match="retry deadline"),
    ):
        await api_client.read("sn", 15, max_retry_time=0.01)
    assert cancelled.is_set()


@pytest.mark.parametrize("request_error", [TimeoutError(), ClientConnectionError("Disconnected")])
async def test_read_retries_a_temporary_request_failure(
    create_api_client: ApiClientFactory, aiohttp_client: AiohttpClient, request_error: Exception
) -> None:
    async def endpoint(_request: web.Request) -> web.Response:
        return web.json_response({"code": "0", "data": {"msg": "55"}})

    app = web.Application()
    app.router.add_post(SolisCloudControlApiClient._READ_ENDPOINT, endpoint)
    client = await aiohttp_client(app)
    api_client = create_api_client(client)
    sleep = AsyncMock()
    retry_policy = RetryPolicy(retryable_exception=SolisCloudControlApiError, sleep=sleep)

    with (
        patch.object(api_client, "_retry_policy", retry_policy),
        patch.object(client, "post", wraps=client.post, side_effect=[request_error, DEFAULT]) as post,
    ):
        assert await api_client.read("sn", 15) == "55"
    assert post.call_count == 2
    sleep.assert_awaited_once_with(1)


@pytest.mark.parametrize("status", [200, 500])
async def test_control_sends_a_failed_command_only_once(
    create_api_client: ApiClientFactory, aiohttp_client: AiohttpClient, status: int
) -> None:
    requests = 0

    async def endpoint(_request: web.Request) -> web.Response:
        nonlocal requests
        requests += 1
        return web.json_response(
            {"code": "0", "data": [{"code": "B0072", "msg": "The device is offline"}]}, status=status
        )

    app = web.Application()
    app.router.add_post(SolisCloudControlApiClient._CONTROL_ENDPOINT, endpoint)
    api_client = create_api_client(await aiohttp_client(app))
    with pytest.raises(SolisCloudControlApiError) as error:
        await api_client.control("sn", 15, "0")
    assert error.value.is_offline == (status == 200)
    assert requests == 1


async def mock_read_batch_endpoint_http_error(request: web.Request) -> web.Response:
    return web.Response(status=500, text="Internal Server Error")


async def mock_read_batch_endpoint_api_error(request: web.Request) -> web.Response:
    return web.json_response({"code": "100", "msg": "API Error"})


async def mock_read_batch_endpoint_missing_data_field(request: web.Request) -> web.Response:
    return web.json_response({"code": "0", "msg": "Success"})


async def mock_read_batch_endpoint_invalid_data_format(request: web.Request) -> web.Response:
    return web.json_response({"code": "0", "msg": "Success", "data": "not an array"})


async def mock_read_batch_endpoint_invalid_outer_data_format(request: web.Request) -> web.Response:
    return web.json_response({"code": "0", "msg": "Success", "data": ["not an array"]})


async def mock_read_batch_endpoint_missing_msg_field(request: web.Request) -> web.Response:
    return web.json_response({"code": "0", "msg": "Success", "data": [[{"cid": -1}]]})


async def mock_read_batch_endpoint_missing_cid_field(request: web.Request) -> web.Response:
    return web.json_response({"code": "0", "msg": "Success", "data": [[{"msg": "any message"}]]})


@pytest.mark.parametrize(
    "mock_endpoint,expected_error",
    [
        (mock_read_batch_endpoint_http_error, SolisCloudControlApiError("Internal Server Error", status_code=500)),
        (
            mock_read_batch_endpoint_api_error,
            SolisCloudControlApiError("API operation failed: API Error", response_code="100"),
        ),
        (
            mock_read_batch_endpoint_missing_data_field,
            SolisCloudControlApiError("ReadBatch failed: missing 'data' field"),
        ),
        (
            mock_read_batch_endpoint_invalid_data_format,
            SolisCloudControlApiError("ReadBatch failed: 'data' field is not an array"),
        ),
        (
            mock_read_batch_endpoint_invalid_outer_data_format,
            SolisCloudControlApiError("ReadBatch failed: 'data' field element is not an array"),
        ),
        (
            mock_read_batch_endpoint_missing_msg_field,
            SolisCloudControlApiError("ReadBatch failed: missing 'msg' field"),
        ),
        (
            mock_read_batch_endpoint_missing_cid_field,
            SolisCloudControlApiError("ReadBatch failed: missing 'cid' field"),
        ),
    ],
)
async def test_read_batch_errors(
    mock_endpoint: MockEndpoint,
    expected_error: SolisCloudControlApiError,
    create_api_client: ApiClientFactory,
    aiohttp_client: AiohttpClient,
) -> None:
    app = web.Application()
    app.router.add_route("POST", SolisCloudControlApiClient._READ_BATCH_ENDPOINT, mock_endpoint)

    client = await aiohttp_client(app)
    api_client = create_api_client(client)

    with pytest.raises(SolisCloudControlApiError) as excinfo:
        await api_client.read_batch(inverter_sn="any inverter", cids=[-1], max_retry_time=0)

    assert str(excinfo.value) == str(expected_error)


async def mock_control_endpoint_http_error(request: web.Request) -> web.Response:
    return web.Response(status=500, text="Internal Server Error")


async def mock_control_endpoint_api_error(request: web.Request) -> web.Response:
    return web.json_response({"code": "100", "msg": "API Error"})


async def mock_control_endpoint_control_error(request: web.Request) -> web.Response:
    return web.json_response({"code": "0", "msg": "Success", "data": [{"code": "100", "msg": "Any Error"}]})


async def mock_control_endpoint_missing_data_field(request: web.Request) -> web.Response:
    return web.json_response({"code": "0", "msg": "Success"})


async def mock_control_endpoint_invalid_data_format(request: web.Request) -> web.Response:
    return web.json_response({"code": "0", "msg": "Success", "data": "not an array"})


@pytest.mark.parametrize(
    "mock_endpoint,expected_error",
    [
        (mock_control_endpoint_http_error, SolisCloudControlApiError("Internal Server Error", status_code=500)),
        (
            mock_control_endpoint_api_error,
            SolisCloudControlApiError("API operation failed: API Error", response_code="100"),
        ),
        (
            mock_control_endpoint_control_error,
            SolisCloudControlApiError("Control failed: Any Error", response_code="100"),
        ),
        (
            mock_control_endpoint_missing_data_field,
            SolisCloudControlApiError("Control failed: missing 'data' field"),
        ),
        (
            mock_control_endpoint_invalid_data_format,
            SolisCloudControlApiError("Control failed: 'data' field is not an array"),
        ),
    ],
)
async def test_control_errors(
    mock_endpoint: MockEndpoint,
    expected_error: SolisCloudControlApiError,
    create_api_client: ApiClientFactory,
    aiohttp_client: AiohttpClient,
) -> None:
    app = web.Application()
    app.router.add_route("POST", SolisCloudControlApiClient._CONTROL_ENDPOINT, mock_endpoint)

    client = await aiohttp_client(app)
    api_client = create_api_client(client)

    with pytest.raises(SolisCloudControlApiError) as excinfo:
        await api_client.control(inverter_sn="any inverter", cid=-1, value="any value")

    assert str(excinfo.value) == str(expected_error)


async def mock_inverter_list_endpoint_missing_data_field(request: web.Request) -> web.Response:
    return web.json_response({"code": "0", "msg": "Success"})


async def mock_inverter_list_endpoint_missing_page(request: web.Request) -> web.Response:
    return web.json_response({"code": "0", "msg": "Success", "data": {}})


async def mock_inverter_list_endpoint_missing_records(request: web.Request) -> web.Response:
    return web.json_response({"code": "0", "msg": "Success", "data": {"page": {}}})


@pytest.mark.parametrize(
    "mock_endpoint,expected_error",
    [
        (mock_read_endpoint_http_error, SolisCloudControlApiError("Internal Server Error", status_code=500)),
        (
            mock_read_endpoint_api_error,
            SolisCloudControlApiError("API operation failed: API Error", response_code="100"),
        ),
        (
            mock_inverter_list_endpoint_missing_data_field,
            SolisCloudControlApiError("InverterList failed: missing 'data' field"),
        ),
        (
            mock_inverter_list_endpoint_missing_page,
            SolisCloudControlApiError("InverterList failed: missing 'page' field"),
        ),
        (
            mock_inverter_list_endpoint_missing_records,
            SolisCloudControlApiError("InverterList failed: missing 'records' field"),
        ),
    ],
)
async def test_inverter_list_errors(
    mock_endpoint: MockEndpoint,
    expected_error: SolisCloudControlApiError,
    create_api_client: ApiClientFactory,
    aiohttp_client: AiohttpClient,
) -> None:
    app = web.Application()
    app.router.add_route("POST", SolisCloudControlApiClient._INVERTER_LIST_ENDPOINT, mock_endpoint)

    client = await aiohttp_client(app)
    api_client = create_api_client(client)

    with pytest.raises(SolisCloudControlApiError) as excinfo:
        await api_client.inverter_list(max_retry_time=0)

    assert str(excinfo.value) == str(expected_error)


async def mock_inverter_details_endpoint_missing_data_field(request: web.Request) -> web.Response:
    return web.json_response({"code": "0", "msg": "Success"})


@pytest.mark.parametrize(
    "mock_endpoint,expected_error",
    [
        (mock_read_endpoint_http_error, SolisCloudControlApiError("Internal Server Error", status_code=500)),
        (
            mock_read_endpoint_api_error,
            SolisCloudControlApiError("API operation failed: API Error", response_code="100"),
        ),
        (
            mock_inverter_details_endpoint_missing_data_field,
            SolisCloudControlApiError("InverterDetails failed: missing 'data' field"),
        ),
    ],
)
async def test_inverter_details_errors(
    mock_endpoint: MockEndpoint,
    expected_error: SolisCloudControlApiError,
    create_api_client: ApiClientFactory,
    aiohttp_client: AiohttpClient,
) -> None:
    app = web.Application()
    app.router.add_route("POST", SolisCloudControlApiClient._INVERTER_DETAILS_ENDPOINT, mock_endpoint)

    client = await aiohttp_client(app)
    api_client = create_api_client(client)

    with pytest.raises(SolisCloudControlApiError) as excinfo:
        await api_client.inverter_details(inverter_sn="any_inverter_sn", max_retry_time=0)

    assert str(excinfo.value) == str(expected_error)
