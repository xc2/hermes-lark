/** Node HTTP transport used by bridge tests. */
const http = require("node:http");

/** Local test-server port supplied by the Python harness. */
const redirectPort = Number(process.env.HERMES_LARK_TEST_HTTP_PORT);

/** Original Node request implementation. */
const originalRequest = http.request;

/** Redirect the synthetic Feishu hostname to the local test server. */
http.request = function redirectedRequest(...args) {
  const requestOptions = args[0];
  if (
    requestOptions &&
    typeof requestOptions === "object" &&
    requestOptions.hostname === "open.feishu.cn"
  ) {
    args[0] = {
      ...requestOptions,
      host: "127.0.0.1",
      hostname: "127.0.0.1",
      port: redirectPort,
    };
  }
  return Reflect.apply(originalRequest, this, args);
};
