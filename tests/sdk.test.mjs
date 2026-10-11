import assert from "node:assert/strict";
import test from "node:test";
import http from "node:http";
import { TracewiseClient } from "../sdk/tracewise.mjs";

test("reject remote telemetry destinations", () => {
  for (const endpoint of ["https://example.com", "http://192.168.1.1:8000", "http://user:pass@127.0.0.1:8000", "http://127.0.0.1:8000/?secret=x"]) {
    assert.throws(() => new TracewiseClient("api", endpoint));
  }
});

test("deliver structured telemetry and preserve application errors", async () => {
  const records = [];
  const server = http.createServer((request, response) => {
    let body = "";
    request.on("data", chunk => body += chunk);
    request.on("end", () => {
      assert.equal(request.url, "/api/ingest");
      records.push(JSON.parse(JSON.parse(body).content));
      response.writeHead(200, {"Content-Type": "application/json"});
      response.end('{"accepted":1}');
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    const client = new TracewiseClient("sdk-api", `http://127.0.0.1:${server.address().port}`);
    assert.equal(await client.event("completed", "INFO", {request_id: "req-sdk"}), true);
    const original = new Error("SDK controlled timeout");
    await assert.rejects(client.capture(async () => { throw original; }), error => error === original);
    assert.equal(records[0].service, "sdk-api");
    assert.equal(records[0].request_id, "req-sdk");
    assert.equal(records[1].level, "ERROR");
    let captured = false;
    try { await client.capture(async () => { throw null; }); }
    catch (error) { captured = true; assert.equal(error, null); }
    assert.equal(captured, true);
    assert.equal(await client.event("x".repeat(70000)), false);
    assert.equal(client.dropped, 1);
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test("redirects fail without following the destination", async () => {
  const server = http.createServer((request, response) => {
    response.writeHead(302, {Location: "http://192.168.1.1/"});
    response.end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    const client = new TracewiseClient("api", `http://127.0.0.1:${server.address().port}`);
    assert.equal(await client.event("redirect test"), false);
    assert.equal(client.dropped, 1);
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test("optional ingest key is sent only to the loopback ingestion endpoint", async () => {
  let received = null;
  const server = http.createServer((request, response) => {
    received = request.headers.authorization;
    request.resume();
    response.writeHead(200, {"Content-Type": "application/json"});
    response.end('{"accepted":1}');
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    const client = new TracewiseClient("api", `http://127.0.0.1:${server.address().port}`, "test-ingest-key");
    assert.equal(await client.event("key delivery"), true);
    assert.equal(received, "Bearer test-ingest-key");
  } finally { await new Promise(resolve => server.close(resolve)); }
});
