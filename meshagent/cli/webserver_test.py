import logging
from pathlib import Path

import pytest
import typer
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


def _write_routes_file(*, path: Path, python_source: str) -> None:
    path.write_text(
        "\n".join(
            [
                "kind: WebServer",
                "version: v1",
                "routes:",
                "  - path: /broken",
                "    methods:",
                "      - GET",
                f"    python: {python_source}",
                "",
            ]
        )
    )


@pytest.mark.asyncio
async def test_runtime_invalid_python_route_returns_500_and_logs_error(
    tmp_path,
    caplog,
) -> None:
    handler_path = tmp_path / "broken.py"
    handler_path.write_text('raise RuntimeError("boom from import")\n')
    routes_path = tmp_path / "webserver.yaml"
    _write_routes_file(path=routes_path, python_source="broken.py")
    config = webserver._load_routes_config_file(routes_path=routes_path)
    app_dir_path = webserver._resolve_app_dir_path(
        routes_path=routes_path, app_dir=None
    )
    with caplog.at_level(logging.ERROR, logger="meshagent.webserver"):
        static_routes, loaded_python_routes = webserver._resolve_runtime_routes(
            config=config,
            routes_path=routes_path,
            app_dir_path=app_dir_path,
        )
        app, _ = webserver._build_web_application(
            static_routes=static_routes,
            loaded_python_routes=loaded_python_routes,
            room=object(),
        )

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get("/broken")

        assert response.status == 500
        assert await response.text() == "Internal Server Error"
    finally:
        await client.close()

    assert "Unable to load python route file" in caplog.text
    assert "RuntimeError: boom from import" in caplog.text


def test_validate_routes_file_fails_when_python_route_does_not_load(tmp_path) -> None:
    handler_path = tmp_path / "broken.py"
    handler_path.write_text('raise RuntimeError("boom from import")\n')
    routes_path = tmp_path / "webserver.yaml"
    _write_routes_file(path=routes_path, python_source="broken.py")

    with pytest.raises(
        typer.BadParameter,
        match=r"Unable to load python route file .*RuntimeError: boom from import",
    ):
        webserver._validate_routes_file(
            routes_file=str(routes_path),
            app_dir=None,
        )


def test_build_webserver_allows_invalid_python_route_file_at_runtime(tmp_path) -> None:
    handler_path = tmp_path / "broken.py"
    handler_path.write_text('raise RuntimeError("boom from import")\n')
    routes_path = tmp_path / "webserver.yaml"
    _write_routes_file(path=routes_path, python_source="broken.py")

    WebServer, host, port = webserver.build_webserver(
        routes_file=str(routes_path),
        default_host="127.0.0.1",
        default_port=8000,
        host_override=None,
        port_override=None,
        app_dir=None,
    )

    assert WebServer is not None
    assert host == "127.0.0.1"
    assert port == 8000


def test_collect_website_upload_files_fails_when_python_route_does_not_load(
    tmp_path,
    monkeypatch,
) -> None:
    handler_path = tmp_path / "broken.py"
    handler_path.write_text('raise RuntimeError("boom from import")\n')
    routes_path = tmp_path / "webserver.yaml"
    _write_routes_file(path=routes_path, python_source="broken.py")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(
        typer.BadParameter,
        match=r"Unable to load python route file .*RuntimeError: boom from import",
    ):
        webserver._collect_website_upload_files(
            routes_file=str(routes_path),
            include_routes_config=True,
            website_mount_path="/website",
            runtime_paths_relative_to_working_dir=True,
            app_dir=None,
        )
