// Lookout demo app: runs normally, and on SIGUSR1 fails in one realistic way (DEMO_MODE).
// Used only by the isolated lookout-demo-* containers; see demo.sh.
"use strict";
const http = require("http");
const net = require("net");

const mode = process.env.DEMO_MODE;
const log = (...args) => console.log(new Date().toISOString(), `[${mode}]`, ...args);

const failures = {
  // Allocate memory until the kernel kills the container (64 MB limit): OOMKilled, exit 137.
  oom() {
    log("loading report cache into memory");
    const cache = [];
    setInterval(() => {
      cache.push(Buffer.alloc(4 * 1024 * 1024, 1));
      log(`report cache: ${cache.length * 4} MB`);
    }, 200);
  },
  // The database it depends on isn't there: three refused connections, then an uncaught error.
  db() {
    log("lost database connection, reconnecting to orders-db:5432");
    const attempt = (n) => {
      const sock = net.connect(5432, "orders-db");
      sock.on("connect", () => { log("connected"); sock.end(); });
      sock.on("error", (err) => {
        if (n < 3) {
          log(`database orders-db:5432 unreachable (attempt ${n}/3): ${err.code}`);
          setTimeout(() => attempt(n + 1), 1000);
        } else {
          log("giving up on orders-db:5432");
          throw err;  // uncaught: Node prints the stack trace and exits 1
        }
      });
    };
    attempt(1);
  },
  // Reload tries to bind the API port that the metrics listener already holds.
  port() {
    log("config reloaded: starting API server on :8080");
    http.createServer((req, res) => res.end("ok")).listen(8080);  // no 'error' handler: EADDRINUSE crashes
  },
};

if (!failures[mode]) {
  console.error(`unknown DEMO_MODE ${mode}`);
  process.exit(2);
}
if (mode === "port") {
  http.createServer((req, res) => res.end("metrics")).listen(8080, () => log("metrics listener on :8080"));
}
log("service started, serving requests");
setInterval(() => log("health check ok"), 20000);
process.on("SIGUSR1", () => failures[mode]());
process.on("SIGTERM", () => { log("shutting down"); process.exit(0); });
