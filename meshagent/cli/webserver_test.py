import pytest
from aiohttp.test_utils import TestClient, TestServer

from meshagent.cli import webserver


async def _start_static_test_client(
    *,
    route_path: str,
    source,
) -> TestClient:
    app, _ = webserver._build_web_application(
        static_routes=[webserver.StaticRoute(path=route_path, source=source)],
        loaded_python_routes=[],
        room=object(),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_static_directory_route_serves_root_index_html(tmp_path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<h1>home</h1>")

    client = await _start_static_test_client(route_path="/", source=dist_dir)
    try:
        response = await client.get("/")

        assert response.status == 200
        assert await response.text() == "<h1>home</h1>"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_static_directory_route_serves_nested_directory_index_html(
    tmp_path,
) -> None:
    dist_dir = tmp_path / "dist"
    docs_dir = dist_dir / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "index.html").write_text("<h1>docs</h1>")

    client = await _start_static_test_client(route_path="/site", source=dist_dir)
    try:
        response = await client.get("/site/docs/")

        assert response.status == 200
        assert await response.text() == "<h1>docs</h1>"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_static_directory_route_returns_not_found_without_directory_index(
    tmp_path,
) -> None:
    dist_dir = tmp_path / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)

    client = await _start_static_test_client(route_path="/", source=dist_dir)
    try:
        response = await client.get("/assets/")

        assert response.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_root_static_directory_route_does_not_override_python_route(
    tmp_path,
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<h1>home</h1>")

    def _handler(*, room, req):
        del room, req
        return webserver.web.Response(text="api")

    app, _ = webserver._build_web_application(
        static_routes=[webserver.StaticRoute(path="/", source=dist_dir)],
        loaded_python_routes=[
            webserver.LoadedPythonRoute(
                path="/api",
                source=tmp_path / "handler.py",
                methods=["GET"],
                handler=_handler,
            )
        ],
        room=object(),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get("/api")

        assert response.status == 200
        assert await response.text() == "api"
    finally:
        await client.close()
