// Preloaded into Epic's signalling server (node --require bind_loopback.cjs dist/index.js ...) for the local
// streaming test: every TCP server it opens (web player page, player and streamer websockets) listens on
// 127.0.0.1 only, so nothing is reachable from the network. Epic's code is not modified.
"use strict";
const net = require("net");

const listen = net.Server.prototype.listen;
net.Server.prototype.listen = function (...args) {
  const first = args[0];
  if (first !== null && typeof first === "object" && !Array.isArray(first)) {
    if (first.path === undefined) args[0] = { ...first, host: "127.0.0.1" };
  } else if (typeof first === "number" || (typeof first === "string" && /^\d+$/.test(first))) {
    if (typeof args[1] === "string" || args[1] == null) args[1] = "127.0.0.1";   // listen(port, host|undefined, ...)
    else args.splice(1, 0, "127.0.0.1");                                       // listen(port, [backlog], [cb])
  }
  return listen.apply(this, args);
};
