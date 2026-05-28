import { createServer } from "node:http";
import { attachRoomWebSocketProxy } from "@meshagent/meshagent-node-ts";
import next from "next";

const hostname = process.env.HOSTNAME || "0.0.0.0";
const port = Number.parseInt(process.env.PORT || "3000", 10);

const app = next({ dev: true, hostname, port });
const handle = app.getRequestHandler();

await app.prepare();
const handleUpgrade = typeof app.getUpgradeHandler === "function" ? app.getUpgradeHandler() : null;

const server = createServer((request, response) => {
  handle(request, response);
});

attachRoomWebSocketProxy(server, {
  onUnhandledUpgrade(request, socket, head) {
    if (handleUpgrade !== null) {
      handleUpgrade(request, socket, head);
      return;
    }
    socket.destroy();
  },
});

server.listen(port, hostname, () => {
  console.log(`Ready on http://${hostname}:${port}`);
});
