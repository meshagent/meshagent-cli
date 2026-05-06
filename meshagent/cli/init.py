from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import sys
import textwrap
from typing import Literal, Mapping, Sequence

import click

from meshagent.cli.meshagent_images import (
    MESHAGENT_IMAGE_PREFIX_TEMPLATE,
    render_meshagent_image_prefix_template,
)
from meshagent.cli.version import __version__


SOURCE_SUFFIXES = {
    ".cs",
    ".dart",
    ".go",
    ".js",
    ".jsx",
    ".py",
    ".rb",
    ".ts",
    ".tsx",
}
PROJECT_MARKER_NAMES = {
    "Containerfile",
    "Dockerfile",
    "Gemfile",
    "go.mod",
    "meshagent.yaml",
    "meshagent.yml",
    "package.json",
    "pubspec.yaml",
    "pyproject.toml",
    "requirements.txt",
    "tsconfig.json",
}
IGNORED_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    "bin",
    "build",
    "dist",
    "node_modules",
    "obj",
    "target",
    "venv",
}
IGNORED_FILE_NAMES = {
    ".DS_Store",
    ".gitkeep",
}

WEB_FOCUS = "webserver"
AGENT_FOCUS = "backend-agent"
DEFAULT_LANGUAGE = "python"
DEFAULT_FOCUS = AGENT_FOCUS


@dataclass(frozen=True, slots=True)
class InitTemplate:
    language_id: str
    focus_id: str
    label: str
    description: str
    files: Mapping[str, str]
    next_steps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InitLanguage:
    id: str
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class InitFocus:
    id: str
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class ExistingProjectSelection:
    action: Literal["run-doctor", "create-subfolder"]
    subfolder_name: str | None = None


PYTHON_WEBSERVER = """\
from __future__ import annotations

import asyncio
import os

from aiohttp import web


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok\\n", content_type="text/plain")


async def index(request: web.Request) -> web.Response:
    return web.Response(
        text="hello from meshagent init\\n",
        content_type="text/plain",
    )


async def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", index)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Serving on 0.0.0.0:{port}")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
"""

PYTHON_AGENT = """\
from __future__ import annotations

import asyncio
import os

from meshagent.api import RoomClient, WebSocketClientProtocol, websocket_room_url


async def main() -> None:
    room_name = os.environ.get("MESHAGENT_ROOM")
    token = os.environ.get("MESHAGENT_TOKEN")
    if not room_name or not token:
        print("MeshAgent room environment is not set; waiting for deployment env.")
        await asyncio.Event().wait()
        return

    protocol = WebSocketClientProtocol(
        url=websocket_room_url(room_name=room_name),
        token=token,
    )
    async with RoomClient(protocol_factory=protocol.create_factory()) as room:
        print(f"Connected to MeshAgent room: {room.room_name}")
        await room.wait_for_close()


if __name__ == "__main__":
    asyncio.run(main())
"""

PYTHON_WEBSERVER_PYPROJECT = """\
[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "meshagent-init-python-webserver"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
  "aiohttp[speedups]~=3.13.0",
]

[tool.setuptools]
py-modules = ["server"]
"""

PYTHON_AGENT_PYPROJECT = f"""\
[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "meshagent-init-python-agent"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
  "meshagent-api=={__version__}",
]

[tool.setuptools]
py-modules = ["server"]
"""

PYTHON_DOCKERFILE = f"""\
ARG MESHAGENT_IMAGE_PREFIX={MESHAGENT_IMAGE_PREFIX_TEMPLATE}
FROM ${{MESHAGENT_IMAGE_PREFIX}}python-sdk-slim:{__version__} AS build
WORKDIR /app
COPY . .
RUN python -m pip install --no-cache-dir --target /out .

FROM scratch
LABEL meshagent.runtime=python
WORKDIR /app
COPY --from=build /out /app
EXPOSE 8000
CMD ["-m", "server"]
"""

PYTHON_AGENT_DOCKERFILE = f"""\
ARG MESHAGENT_IMAGE_PREFIX={MESHAGENT_IMAGE_PREFIX_TEMPLATE}
FROM ${{MESHAGENT_IMAGE_PREFIX}}python-sdk-slim:{__version__} AS build
WORKDIR /app
COPY . .
RUN python -m pip install --no-cache-dir --target /out .

FROM scratch
LABEL meshagent.runtime=python
WORKDIR /app
COPY --from=build /out /app
CMD ["-m", "server"]
"""

PYTHON_DOCKERIGNORE = """\
__pycache__/
*.pyc
.pytest_cache/
.venv/
venv/
.git/
.DS_Store
"""

JAVASCRIPT_PACKAGE_JSON = """\
{
  "name": "meshagent-init-javascript",
  "version": "0.1.0",
  "private": true,
  "type": "commonjs",
  "scripts": {
    "build": "ncc build server.js -o dist",
    "start": "node dist/index.js"
  },
  "devDependencies": {
    "@vercel/ncc": "^0.38.3"
  }
}
"""

JAVASCRIPT_AGENT_PACKAGE_JSON = f"""\
{{
  "name": "meshagent-init-javascript-agent",
  "version": "0.1.0",
  "private": true,
  "type": "commonjs",
  "scripts": {{
    "build": "ncc build server.js -o dist",
    "start": "node dist/index.js"
  }},
  "dependencies": {{
    "@meshagent/meshagent": "^{__version__}"
  }},
  "devDependencies": {{
    "@vercel/ncc": "^0.38.3"
  }}
}}
"""

JAVASCRIPT_WEBSERVER = """\
const http = require("node:http");

const port = Number(process.env.PORT || 3000);

const server = http.createServer((request, response) => {
  if (request.url === "/health") {
    response.writeHead(200, { "content-type": "text/plain; charset=utf-8" });
    response.end("ok\\n");
    return;
  }

  if (request.url === "/") {
    response.writeHead(200, { "content-type": "text/plain; charset=utf-8" });
    response.end("hello from meshagent init\\n");
    return;
  }

  response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
  response.end("not found\\n");
});

server.listen(port, "0.0.0.0");
"""

JAVASCRIPT_AGENT = """\
const { RoomClient } = require("@meshagent/meshagent");

async function main() {
  if (!process.env.MESHAGENT_ROOM || !process.env.MESHAGENT_TOKEN) {
    console.log("MeshAgent room environment is not set; waiting for deployment env.");
    await new Promise(() => {});
    return;
  }

  const room = new RoomClient();
  await room.start();
  console.log(`Connected to MeshAgent room: ${room.roomName}`);
  await new Promise(() => {});
}

main().catch((error) => {
  console.error("Unable to start MeshAgent RoomClient:", error);
  process.exitCode = 1;
});
"""

JAVASCRIPT_DOCKERFILE = f"""\
ARG MESHAGENT_IMAGE_PREFIX={MESHAGENT_IMAGE_PREFIX_TEMPLATE}
FROM ${{MESHAGENT_IMAGE_PREFIX}}node-sdk:{__version__} AS build
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY server.js ./
RUN npm run build

FROM scratch
LABEL meshagent.runtime=node
WORKDIR /app
COPY --from=build /app/dist/index.js /app/index.js
EXPOSE 3000
CMD ["index.js"]
"""

JAVASCRIPT_AGENT_DOCKERFILE = f"""\
ARG MESHAGENT_IMAGE_PREFIX={MESHAGENT_IMAGE_PREFIX_TEMPLATE}
FROM ${{MESHAGENT_IMAGE_PREFIX}}node-sdk:{__version__} AS build
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY server.js ./
RUN npm run build

FROM scratch
LABEL meshagent.runtime=node
WORKDIR /app
COPY --from=build /app/dist/index.js /app/index.js
CMD ["index.js"]
"""

JAVASCRIPT_DOCKERIGNORE = """\
node_modules/
dist/
npm-debug.log*
.git/
.DS_Store
"""

TYPESCRIPT_PACKAGE_JSON = """\
{
  "name": "meshagent-init-typescript",
  "version": "0.1.0",
  "private": true,
  "type": "commonjs",
  "scripts": {
    "build": "ncc build src/server.ts -o dist",
    "start": "node dist/index.js"
  },
  "devDependencies": {
    "@types/node": "^22.10.0",
    "@vercel/ncc": "^0.38.3",
    "typescript": "^5.8.0"
  }
}
"""

TYPESCRIPT_AGENT_PACKAGE_JSON = f"""\
{{
  "name": "meshagent-init-typescript-agent",
  "version": "0.1.0",
  "private": true,
  "type": "commonjs",
  "scripts": {{
    "build": "ncc build src/server.ts -o dist",
    "start": "node dist/index.js"
  }},
  "dependencies": {{
    "@meshagent/meshagent": "^{__version__}"
  }},
  "devDependencies": {{
    "@types/node": "^22.10.0",
    "@vercel/ncc": "^0.38.3",
    "typescript": "^5.8.0"
  }}
}}
"""

TYPESCRIPT_TSCONFIG = """\
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "CommonJS",
    "moduleResolution": "Node",
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "types": ["node"]
  },
  "include": ["src/**/*.ts"]
}
"""

TYPESCRIPT_WEBSERVER = """\
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";

const port = Number(process.env.PORT || 3000);

const server = createServer((request: IncomingMessage, response: ServerResponse) => {
  if (request.url === "/health") {
    response.writeHead(200, { "content-type": "text/plain; charset=utf-8" });
    response.end("ok\\n");
    return;
  }

  if (request.url === "/") {
    response.writeHead(200, { "content-type": "text/plain; charset=utf-8" });
    response.end("hello from meshagent init\\n");
    return;
  }

  response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
  response.end("not found\\n");
});

server.listen(port, "0.0.0.0");
"""

TYPESCRIPT_AGENT = """\
import { RoomClient } from "@meshagent/meshagent";

async function main(): Promise<void> {
  if (!process.env.MESHAGENT_ROOM || !process.env.MESHAGENT_TOKEN) {
    console.log("MeshAgent room environment is not set; waiting for deployment env.");
    await new Promise<never>(() => {});
    return;
  }

  const room = new RoomClient();
  await room.start();
  console.log(`Connected to MeshAgent room: ${room.roomName}`);
  await new Promise<never>(() => {});
}

main().catch((error: unknown) => {
  console.error("Unable to start MeshAgent RoomClient:", error);
  process.exitCode = 1;
});
"""

TYPESCRIPT_DOCKERFILE = f"""\
ARG MESHAGENT_IMAGE_PREFIX={MESHAGENT_IMAGE_PREFIX_TEMPLATE}
FROM ${{MESHAGENT_IMAGE_PREFIX}}node-sdk:{__version__} AS build
WORKDIR /app
COPY package*.json tsconfig.json ./
RUN npm install
COPY src ./src
RUN npm run build

FROM scratch
LABEL meshagent.runtime=node
WORKDIR /app
COPY --from=build /app/dist/index.js /app/index.js
EXPOSE 3000
CMD ["index.js"]
"""

TYPESCRIPT_AGENT_DOCKERFILE = f"""\
ARG MESHAGENT_IMAGE_PREFIX={MESHAGENT_IMAGE_PREFIX_TEMPLATE}
FROM ${{MESHAGENT_IMAGE_PREFIX}}node-sdk:{__version__} AS build
WORKDIR /app
COPY package*.json tsconfig.json ./
RUN npm install
COPY src ./src
RUN npm run build

FROM scratch
LABEL meshagent.runtime=node
WORKDIR /app
COPY --from=build /app/dist/index.js /app/index.js
CMD ["index.js"]
"""

REACT_PACKAGE_JSON = """\
{
  "name": "meshagent-init-react",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "build": "vite build"
  },
  "dependencies": {
    "@vitejs/plugin-react": "^4.3.4",
    "vite": "^6.0.0",
    "typescript": "^5.8.0",
    "react": "^19.0.0",
    "react-dom": "^19.0.0"
  },
  "devDependencies": {}
}
"""

REACT_TSCONFIG = """\
{
  "compilerOptions": {
    "target": "ES2022",
    "useDefineForClassFields": true,
    "lib": ["DOM", "DOM.Iterable", "ES2022"],
    "allowJs": false,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true,
    "strict": true,
    "forceConsistentCasingInFileNames": true,
    "module": "ESNext",
    "moduleResolution": "Node",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx"
  },
  "include": ["src"]
}
"""

REACT_VITE_CONFIG = """\
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
});
"""

REACT_INDEX_HTML = """\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>MeshAgent Init</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
"""

REACT_MAIN = """\
import React from "react";
import { createRoot } from "react-dom/client";

function App() {
  return <main>hello from meshagent init</main>;
}

const root = document.getElementById("root");
if (root === null) {
  throw new Error("Root element was not found");
}

createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
"""

REACT_DOCKERFILE = """\
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json tsconfig.json vite.config.ts index.html ./
RUN npm install
COPY src ./src
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
RUN rm -f /etc/nginx/conf.d/default.conf && printf '%s\\n' \\
  'pid /data/nginx/nginx.pid;' \\
  'events {}' \\
  'http {' \\
  '  include /etc/nginx/mime.types;' \\
  '  client_body_temp_path /data/nginx/client_temp;' \\
  '  proxy_temp_path /data/nginx/proxy_temp;' \\
  '  fastcgi_temp_path /data/nginx/fastcgi_temp;' \\
  '  uwsgi_temp_path /data/nginx/uwsgi_temp;' \\
  '  scgi_temp_path /data/nginx/scgi_temp;' \\
  '  server { listen 80; location = /health { return 200 "ok\\n"; } location / { try_files $uri $uri/ /index.html; } }' \\
  '}' > /etc/nginx/nginx.conf
EXPOSE 80
CMD ["sh", "-c", "mkdir -p /data/nginx/client_temp /data/nginx/proxy_temp /data/nginx/fastcgi_temp /data/nginx/uwsgi_temp /data/nginx/scgi_temp && nginx -c /etc/nginx/nginx.conf -g 'daemon off;'"]
"""

REACT_DOCKERIGNORE = """\
node_modules/
dist/
npm-debug.log*
.git/
.DS_Store
"""

DOTNET_CSPROJ = """\
<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net9.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
  </PropertyGroup>
</Project>
"""

DOTNET_AGENT_CSPROJ = f"""\
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net9.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Meshagent.Api" Version="{__version__}" />
  </ItemGroup>
</Project>
"""

DOTNET_PROGRAM = """\
var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();

var port = Environment.GetEnvironmentVariable("PORT") ?? "5000";
app.Urls.Clear();
app.Urls.Add($"http://0.0.0.0:{port}");

app.MapGet("/health", () => Results.Text("ok\\n", "text/plain"));
app.MapGet("/", () => Results.Text("hello from meshagent init\\n", "text/plain"));

await app.RunAsync();
"""

DOTNET_AGENT_PROGRAM = """\
using Meshagent.Api.Room;

if (
    string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("MESHAGENT_ROOM"))
    || string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("MESHAGENT_TOKEN"))
)
{
    Console.WriteLine("MeshAgent room environment is not set; waiting for deployment env.");
    await Task.Delay(Timeout.InfiniteTimeSpan);
    return;
}

await using var room = new RoomClient();
await room.ConnectAsync();
Console.WriteLine($"Connected to MeshAgent room: {room.RoomName}");
await Task.Delay(Timeout.InfiniteTimeSpan);
"""

DOTNET_DOCKERFILE = """\
FROM mcr.microsoft.com/dotnet/sdk:9.0 AS build
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1
ENV DOTNET_NOLOGO=1
WORKDIR /src
COPY MeshAgentHello.csproj ./
RUN dotnet restore
COPY Program.cs ./
RUN dotnet publish -c Release -o /app/publish --no-restore --disable-build-servers /p:UseSharedCompilation=false

FROM mcr.microsoft.com/dotnet/aspnet:9.0
WORKDIR /app
COPY --from=build /app/publish .
EXPOSE 5000
ENTRYPOINT ["dotnet", "MeshAgentHello.dll"]
"""

DOTNET_AGENT_DOCKERFILE = """\
FROM mcr.microsoft.com/dotnet/sdk:9.0 AS build
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1
ENV DOTNET_NOLOGO=1
WORKDIR /src
COPY MeshAgentHello.csproj ./
RUN dotnet restore
COPY Program.cs ./
RUN dotnet publish -c Release -o /app/publish --no-restore --disable-build-servers /p:UseSharedCompilation=false

FROM mcr.microsoft.com/dotnet/runtime:9.0
WORKDIR /app
COPY --from=build /app/publish .
ENTRYPOINT ["dotnet", "MeshAgentHello.dll"]
"""

DOTNET_DOCKERIGNORE = """\
bin/
obj/
.git/
.DS_Store
"""

DART_AGENT_PUBSPEC = f"""\
name: meshagent_init_dart_agent
publish_to: "none"
environment:
  sdk: ">=3.8.0 <4.0.0"
dependencies:
  meshagent: ^{__version__}
"""

DART_AGENT = """\
import 'dart:async';
import 'dart:io';

import 'package:meshagent/meshagent.dart';

Future<void> main() async {
  final roomName = Platform.environment['MESHAGENT_ROOM'];
  final token = Platform.environment['MESHAGENT_TOKEN'];
  if (roomName == null || roomName.isEmpty || token == null || token.isEmpty) {
    print('MeshAgent room environment is not set; waiting for deployment env.');
    await Completer<void>().future;
    return;
  }

  final room = RoomClient(
    protocolFactory: WebSocketClientProtocol.createFactory(
      url: _websocketRoomUrl(roomName),
      token: token,
    ),
  );
  await room.start();
  print('Connected to MeshAgent room: ${room.roomName}');
  await Completer<void>().future;
}

Uri _websocketRoomUrl(String roomName) {
  var baseUrl = Platform.environment['MESHAGENT_ROOM_URL'] ??
      Platform.environment['MESHAGENT_API_URL'] ??
      'https://api.meshagent.com';
  if (baseUrl.startsWith('https:')) {
    baseUrl = 'wss:${baseUrl.substring('https:'.length)}';
  } else if (baseUrl.startsWith('http:')) {
    baseUrl = 'ws:${baseUrl.substring('http:'.length)}';
  }
  return Uri.parse('$baseUrl/rooms/$roomName');
}

"""

DART_AGENT_DOCKERFILE = """\
FROM dart:stable
WORKDIR /app
COPY pubspec.yaml ./
RUN dart pub get
COPY bin ./bin
CMD ["dart", "run", "bin/server.dart"]
"""

DART_DOCKERIGNORE = """\
.dart_tool/
build/
.git/
.DS_Store
"""

FLUTTER_PUBSPEC = """\
name: meshagent_init_flutter
description: A minimal deployable Flutter web app for MeshAgent.
publish_to: "none"
environment:
  sdk: ">=3.8.0 <4.0.0"
dependencies:
  flutter:
    sdk: flutter
"""

FLUTTER_MAIN = """\
import 'package:flutter/material.dart';

void main() {
  runApp(const MeshAgentInitApp());
}

class MeshAgentInitApp extends StatelessWidget {
  const MeshAgentInitApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'MeshAgent Init',
      home: Scaffold(
        body: Center(
          child: Text(
            'hello from meshagent init',
            style: Theme.of(context).textTheme.headlineMedium,
          ),
        ),
      ),
    );
  }
}
"""

FLUTTER_INDEX_HTML = """\
<!doctype html>
<html>
<head>
  <base href="$FLUTTER_BASE_HREF">
  <meta charset="UTF-8">
  <meta content="IE=Edge" http-equiv="X-UA-Compatible">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MeshAgent Init</title>
</head>
<body>
  <script src="flutter_bootstrap.js" async></script>
</body>
</html>
"""

FLUTTER_DOCKERFILE = """\
FROM ghcr.io/cirruslabs/flutter:stable AS build
WORKDIR /app
COPY pubspec.yaml ./
RUN flutter pub get
COPY lib ./lib
COPY web ./web
RUN flutter build web --release

FROM nginx:1.27-alpine
COPY --from=build /app/build/web /usr/share/nginx/html
RUN rm -f /etc/nginx/conf.d/default.conf && printf '%s\\n' \\
  'pid /data/nginx/nginx.pid;' \\
  'events {}' \\
  'http {' \\
  '  include /etc/nginx/mime.types;' \\
  '  client_body_temp_path /data/nginx/client_temp;' \\
  '  proxy_temp_path /data/nginx/proxy_temp;' \\
  '  fastcgi_temp_path /data/nginx/fastcgi_temp;' \\
  '  uwsgi_temp_path /data/nginx/uwsgi_temp;' \\
  '  scgi_temp_path /data/nginx/scgi_temp;' \\
  '  server { listen 80; location = /health { return 200 "ok\\n"; } location / { try_files $uri $uri/ /index.html; } }' \\
  '}' > /etc/nginx/nginx.conf
EXPOSE 80
CMD ["sh", "-c", "mkdir -p /data/nginx/client_temp /data/nginx/proxy_temp /data/nginx/fastcgi_temp /data/nginx/uwsgi_temp /data/nginx/scgi_temp && nginx -c /etc/nginx/nginx.conf -g 'daemon off;'"]
"""

FLUTTER_DOCKERIGNORE = """\
.dart_tool/
build/
.flutter-plugins
.flutter-plugins-dependencies
.git/
.DS_Store
"""

WEBSERVER_NEXT_STEPS = (
    "meshagent doctor",
    "meshagent deploy . --tag <repository>:<tag> --public --liveness /health --wait",
)
STATIC_WEBSERVER_NEXT_STEPS = (
    "meshagent doctor",
    "meshagent deploy . --tag <repository>:<tag> --public --liveness /health --room-mount /:/data:rw --wait",
)
AGENT_NEXT_STEPS = (
    "meshagent doctor",
    (
        "meshagent deploy . --tag <repository>:<tag> "
        "--meshagent-token agentDefault --wait"
    ),
)

LANGUAGES: Mapping[str, InitLanguage] = {
    "python": InitLanguage(
        id="python",
        label="Python",
        description="Python 3.13.",
    ),
    "javascript": InitLanguage(
        id="javascript",
        label="JavaScript",
        description="Node.js/CommonJS.",
    ),
    "typescript": InitLanguage(
        id="typescript",
        label="TypeScript",
        description="Node.js/TypeScript.",
    ),
    "react": InitLanguage(
        id="react",
        label="React",
        description="React/Vite.",
    ),
    "dotnet": InitLanguage(
        id="dotnet",
        label=".NET",
        description=".NET.",
    ),
    "dart-flutter": InitLanguage(
        id="dart-flutter",
        label="Dart/Flutter",
        description="Dart or Flutter.",
    ),
}

FOCUSES: Mapping[str, InitFocus] = {
    WEB_FOCUS: InitFocus(
        id=WEB_FOCUS,
        label="Web server",
        description="HTTP app with a health endpoint and public route.",
    ),
    AGENT_FOCUS: InitFocus(
        id=AGENT_FOCUS,
        label="Backend agent",
        description="Headless RoomClient SDK service without a public port.",
    ),
}

TEMPLATES: Mapping[tuple[str, str], InitTemplate] = {
    ("python", WEB_FOCUS): InitTemplate(
        language_id="python",
        focus_id=WEB_FOCUS,
        label="Python web server",
        description="Async Python HTTP service on a declared container port.",
        files={
            "pyproject.toml": PYTHON_WEBSERVER_PYPROJECT,
            "server.py": PYTHON_WEBSERVER,
            "Dockerfile": PYTHON_DOCKERFILE,
            ".dockerignore": PYTHON_DOCKERIGNORE,
        },
        next_steps=WEBSERVER_NEXT_STEPS,
    ),
    ("python", AGENT_FOCUS): InitTemplate(
        language_id="python",
        focus_id=AGENT_FOCUS,
        label="Python backend agent",
        description="Headless Python RoomClient service.",
        files={
            "pyproject.toml": PYTHON_AGENT_PYPROJECT,
            "server.py": PYTHON_AGENT,
            "Dockerfile": PYTHON_AGENT_DOCKERFILE,
            ".dockerignore": PYTHON_DOCKERIGNORE,
        },
        next_steps=AGENT_NEXT_STEPS,
    ),
    ("javascript", WEB_FOCUS): InitTemplate(
        language_id="javascript",
        focus_id=WEB_FOCUS,
        label="JavaScript web server",
        description="Node.js HTTP service on a declared container port.",
        files={
            "package.json": JAVASCRIPT_PACKAGE_JSON,
            "server.js": JAVASCRIPT_WEBSERVER,
            "Dockerfile": JAVASCRIPT_DOCKERFILE,
            ".dockerignore": JAVASCRIPT_DOCKERIGNORE,
        },
        next_steps=WEBSERVER_NEXT_STEPS,
    ),
    ("javascript", AGENT_FOCUS): InitTemplate(
        language_id="javascript",
        focus_id=AGENT_FOCUS,
        label="JavaScript backend agent",
        description="Headless Node.js RoomClient service.",
        files={
            "package.json": JAVASCRIPT_AGENT_PACKAGE_JSON,
            "server.js": JAVASCRIPT_AGENT,
            "Dockerfile": JAVASCRIPT_AGENT_DOCKERFILE,
            ".dockerignore": JAVASCRIPT_DOCKERIGNORE,
        },
        next_steps=AGENT_NEXT_STEPS,
    ),
    ("typescript", WEB_FOCUS): InitTemplate(
        language_id="typescript",
        focus_id=WEB_FOCUS,
        label="TypeScript web server",
        description="Node.js TypeScript HTTP service on a declared container port.",
        files={
            "package.json": TYPESCRIPT_PACKAGE_JSON,
            "tsconfig.json": TYPESCRIPT_TSCONFIG,
            "src/server.ts": TYPESCRIPT_WEBSERVER,
            "Dockerfile": TYPESCRIPT_DOCKERFILE,
            ".dockerignore": JAVASCRIPT_DOCKERIGNORE,
        },
        next_steps=WEBSERVER_NEXT_STEPS,
    ),
    ("typescript", AGENT_FOCUS): InitTemplate(
        language_id="typescript",
        focus_id=AGENT_FOCUS,
        label="TypeScript backend agent",
        description="Headless TypeScript RoomClient service.",
        files={
            "package.json": TYPESCRIPT_AGENT_PACKAGE_JSON,
            "tsconfig.json": TYPESCRIPT_TSCONFIG,
            "src/server.ts": TYPESCRIPT_AGENT,
            "Dockerfile": TYPESCRIPT_AGENT_DOCKERFILE,
            ".dockerignore": JAVASCRIPT_DOCKERIGNORE,
        },
        next_steps=AGENT_NEXT_STEPS,
    ),
    ("react", WEB_FOCUS): InitTemplate(
        language_id="react",
        focus_id=WEB_FOCUS,
        label="React web server",
        description="React/Vite web app served by nginx on a declared container port.",
        files={
            "package.json": REACT_PACKAGE_JSON,
            "tsconfig.json": REACT_TSCONFIG,
            "vite.config.ts": REACT_VITE_CONFIG,
            "index.html": REACT_INDEX_HTML,
            "src/main.tsx": REACT_MAIN,
            "Dockerfile": REACT_DOCKERFILE,
            ".dockerignore": REACT_DOCKERIGNORE,
        },
        next_steps=STATIC_WEBSERVER_NEXT_STEPS,
    ),
    ("dotnet", WEB_FOCUS): InitTemplate(
        language_id="dotnet",
        focus_id=WEB_FOCUS,
        label=".NET web server",
        description="ASP.NET Core HTTP service on a declared container port.",
        files={
            "MeshAgentHello.csproj": DOTNET_CSPROJ,
            "Program.cs": DOTNET_PROGRAM,
            "Dockerfile": DOTNET_DOCKERFILE,
            ".dockerignore": DOTNET_DOCKERIGNORE,
        },
        next_steps=WEBSERVER_NEXT_STEPS,
    ),
    ("dotnet", AGENT_FOCUS): InitTemplate(
        language_id="dotnet",
        focus_id=AGENT_FOCUS,
        label=".NET backend agent",
        description="Headless .NET RoomClient service.",
        files={
            "MeshAgentHello.csproj": DOTNET_AGENT_CSPROJ,
            "Program.cs": DOTNET_AGENT_PROGRAM,
            "Dockerfile": DOTNET_AGENT_DOCKERFILE,
            ".dockerignore": DOTNET_DOCKERIGNORE,
        },
        next_steps=AGENT_NEXT_STEPS,
    ),
    ("dart-flutter", WEB_FOCUS): InitTemplate(
        language_id="dart-flutter",
        focus_id=WEB_FOCUS,
        label="Flutter web server",
        description="Flutter web app served by nginx on a declared container port.",
        files={
            "pubspec.yaml": FLUTTER_PUBSPEC,
            "lib/main.dart": FLUTTER_MAIN,
            "web/index.html": FLUTTER_INDEX_HTML,
            "Dockerfile": FLUTTER_DOCKERFILE,
            ".dockerignore": FLUTTER_DOCKERIGNORE,
        },
        next_steps=STATIC_WEBSERVER_NEXT_STEPS,
    ),
    ("dart-flutter", AGENT_FOCUS): InitTemplate(
        language_id="dart-flutter",
        focus_id=AGENT_FOCUS,
        label="Dart backend agent",
        description="Headless Dart RoomClient service.",
        files={
            "pubspec.yaml": DART_AGENT_PUBSPEC,
            "bin/server.dart": DART_AGENT,
            "Dockerfile": DART_AGENT_DOCKERFILE,
            ".dockerignore": DART_DOCKERIGNORE,
        },
        next_steps=AGENT_NEXT_STEPS,
    ),
}

LANGUAGE_ALIASES = {
    ".net": "dotnet",
    "c#": "dotnet",
    "csharp": "dotnet",
    "dart": "dart-flutter",
    "dart/flutter": "dart-flutter",
    "dart-flutter": "dart-flutter",
    "dotnet": "dotnet",
    "flutter": "dart-flutter",
    "javascript": "javascript",
    "js": "javascript",
    "node": "javascript",
    "node.js": "javascript",
    "nodejs": "javascript",
    "python": "python",
    "py": "python",
    "react": "react",
    "react.js": "react",
    "reactjs": "react",
    "react-vite": "react",
    "ts": "typescript",
    "typescript": "typescript",
    "node-ts": "typescript",
    "node.ts": "typescript",
    "vite-react": "react",
}
FOCUS_ALIASES = {
    "agent": AGENT_FOCUS,
    "backend": AGENT_FOCUS,
    "backend-agent": AGENT_FOCUS,
    "backend_agent": AGENT_FOCUS,
    "roomclient": AGENT_FOCUS,
    "room-client": AGENT_FOCUS,
    "web": WEB_FOCUS,
    "webserver": WEB_FOCUS,
    "web-server": WEB_FOCUS,
    "web_server": WEB_FOCUS,
    "webservering": WEB_FOCUS,
    "webserving": WEB_FOCUS,
}


def _has_existing_project_content(root: Path) -> bool:
    for path in sorted(root.rglob("*")):
        try:
            relative_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in IGNORED_DIR_NAMES for part in relative_parts[:-1]):
            continue
        if path.is_dir():
            continue
        if path.name in IGNORED_FILE_NAMES:
            continue
        if path.name in PROJECT_MARKER_NAMES:
            return True
        if path.suffix.lower() in SOURCE_SUFFIXES:
            return True
    return False


def _write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered_contents = render_meshagent_image_prefix_template(
        textwrap.dedent(contents).lstrip()
    )
    path.write_text(rendered_contents, encoding="utf-8")


def _supported_language_text() -> str:
    return ", ".join(language.id for language in LANGUAGES.values())


def _supported_focus_text() -> str:
    return ", ".join(focus.id for focus in FOCUSES.values())


def _resolve_language_id(language: str | None) -> str:
    if language is None or language.strip() == "":
        return DEFAULT_LANGUAGE

    normalized = language.strip().lower()
    language_id = LANGUAGE_ALIASES.get(normalized)
    if language_id is None:
        expected = _supported_language_text()
        raise click.ClickException(
            f"Unsupported language: {language}. Expected one of: {expected}."
        )
    return language_id


def _resolve_focus_id(focus: str | None) -> str:
    if focus is None or focus.strip() == "":
        return DEFAULT_FOCUS

    normalized = focus.strip().lower()
    focus_id = FOCUS_ALIASES.get(normalized)
    if focus_id is None:
        expected = _supported_focus_text()
        raise click.ClickException(
            f"Unsupported focus: {focus}. Expected one of: {expected}."
        )
    return focus_id


def _resolve_template(language_id: str, focus_id: str) -> InitTemplate:
    template = TEMPLATES.get((language_id, focus_id))
    if template is not None:
        return template

    language = LANGUAGES[language_id]
    supported = [
        focus
        for template_language_id, focus in TEMPLATES
        if template_language_id == language_id
    ]
    supported_text = ", ".join(supported) if supported else "none"
    raise click.ClickException(
        f"Unsupported template combination: {language.label} does not support "
        f"{focus_id}. Supported focus: {supported_text}."
    )


def _stdio_is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _should_launch_tui(
    *,
    language: str | None,
    focus: str | None,
    interactive: bool | None,
    stdin_is_tty: bool,
    stdout_is_tty: bool,
) -> bool:
    if interactive is False:
        return False
    if interactive is True:
        return stdin_is_tty and stdout_is_tty
    return (language is None or focus is None) and stdin_is_tty and stdout_is_tty


def _supported_focus_ids_for_language(language_id: str) -> tuple[str, ...]:
    return tuple(
        focus_id
        for template_language_id, focus_id in TEMPLATES
        if template_language_id == language_id
    )


def _language_choices() -> Sequence[tuple[str, str, str, tuple[str, ...]]]:
    return tuple(
        (
            language.id,
            language.label,
            language.description,
            _supported_focus_ids_for_language(language.id),
        )
        for language in LANGUAGES.values()
    )


def _focus_choices() -> Sequence[tuple[str, str, str]]:
    return tuple(
        (focus.id, focus.label, focus.description) for focus in FOCUSES.values()
    )


def _run_init_tui(
    *,
    language_choices: Sequence[tuple[str, str, str, tuple[str, ...]]],
    focus_choices: Sequence[tuple[str, str, str]],
) -> tuple[str, str] | None:
    from meshagent.cli.tui.init import (
        InitFocusChoice,
        InitLanguageChoice,
        run_init_wizard_tui,
    )

    languages = [
        InitLanguageChoice(
            id=language_id,
            label=label,
            description=description,
            focus_ids=focus_ids,
        )
        for language_id, label, description, focus_ids in language_choices
    ]
    focuses = [
        InitFocusChoice(id=focus_id, label=label, description=description)
        for focus_id, label, description in focus_choices
    ]
    result = asyncio.run(run_init_wizard_tui(languages=languages, focuses=focuses))
    if result.status != "completed":
        return None
    if result.selected_language_id is None or result.selected_focus_id is None:
        return None
    return result.selected_language_id, result.selected_focus_id


def _run_existing_project_tui() -> ExistingProjectSelection | None:
    from meshagent.cli.tui.init import run_existing_project_init_tui

    result = asyncio.run(run_existing_project_init_tui())
    if result.status != "completed" or result.action is None:
        return None
    return ExistingProjectSelection(
        action=result.action,
        subfolder_name=result.subfolder_name,
    )


def _validate_subfolder_name(folder_name: str | None) -> str:
    if folder_name is None:
        raise click.ClickException("Folder name cannot be empty.")

    resolved_folder_name = folder_name.strip()
    if resolved_folder_name == "":
        raise click.ClickException("Folder name cannot be empty.")
    if (
        resolved_folder_name in {".", ".."}
        or "/" in resolved_folder_name
        or "\\" in resolved_folder_name
    ):
        raise click.ClickException("Folder name must be a single new subfolder name.")
    return resolved_folder_name


def _new_project_subfolder(root: Path, folder_name: str | None) -> Path:
    resolved_folder_name = _validate_subfolder_name(folder_name)
    target = root / resolved_folder_name
    if target.exists():
        raise click.ClickException(f"Subfolder already exists: {target}")
    return target


def _run_doctor(root: Path) -> None:
    from meshagent.cli.doctor import _print_report, diagnose_project

    _print_report(diagnose_project(root))


def _write_template(root: Path, template: InitTemplate) -> None:
    for name, contents in template.files.items():
        _write_file(root / name, contents)


def _print_created_report(*, template: InitTemplate) -> None:
    click.echo("")
    click.echo(f"Created a minimal deployable {template.label} hello world project:")
    for name in template.files:
        click.echo(f"  {name}")
    click.echo("")
    click.echo("Next steps:")
    for step in template.next_steps:
        click.echo(f"  {step}")


@click.command(
    "init",
    help="Create a minimal deployable hello world project.",
)
@click.option(
    "--language",
    "-l",
    type=str,
    default=None,
    help=(
        "Template language for non-interactive use. "
        "Supported: python, javascript, typescript, react, dotnet, dart/flutter."
    ),
)
@click.option(
    "--focus",
    type=str,
    default=None,
    help=(
        "Project focus for non-interactive use. Supported: webserver, backend-agent."
    ),
)
@click.option(
    "--interactive/--no-interactive",
    default=None,
    help=(
        "Run or bypass the interactive template picker. Defaults to interactive "
        "when attached to a TTY and language or focus is missing."
    ),
)
@click.argument(
    "path",
    required=False,
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
)
def init_command(
    path: Path | None = None,
    language: str | None = None,
    focus: str | None = None,
    interactive: bool | None = None,
) -> None:
    """Create a minimal project that can be deployed on MeshAgent."""

    root = (path or Path.cwd()).resolve()
    root.mkdir(parents=True, exist_ok=True)

    click.echo("meshagent init")
    click.echo(f"Project: {root}")

    is_interactive_stdio = _stdio_is_interactive()
    if interactive is True and not is_interactive_stdio:
        raise click.ClickException(
            "Interactive mode requires a TTY. Pass --language, --focus, and "
            "--no-interactive when running from a script."
        )

    if _has_existing_project_content(root):
        if interactive is not False and is_interactive_stdio:
            existing_project_selection = _run_existing_project_tui()
            if existing_project_selection is None:
                click.echo("Init canceled.")
                return
            if existing_project_selection.action == "run-doctor":
                click.echo("")
                _run_doctor(root)
                return

            root = _new_project_subfolder(
                root,
                existing_project_selection.subfolder_name,
            )
            root.mkdir(parents=True, exist_ok=False)
            click.echo(f"New project: {root}")
        else:
            click.echo("")
            click.echo("Existing application code or deployment metadata was detected.")
            click.echo("No files were written.")
            click.echo("")
            click.echo("Recommended next step for existing projects:")
            click.echo("  meshagent doctor")
            return

    if _should_launch_tui(
        language=language,
        focus=focus,
        interactive=interactive,
        stdin_is_tty=is_interactive_stdio,
        stdout_is_tty=is_interactive_stdio,
    ):
        selection = _run_init_tui(
            language_choices=_language_choices(),
            focus_choices=_focus_choices(),
        )
        if selection is None:
            click.echo("Init canceled.")
            return
        language_id, focus_id = selection
    else:
        language_id = _resolve_language_id(language)
        focus_id = _resolve_focus_id(focus)

    template = _resolve_template(language_id, focus_id)
    _write_template(root, template)
    _print_created_report(template=template)
