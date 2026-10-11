import http from "node:http";

export class TracewiseClient {
  constructor(service, endpoint = "http://127.0.0.1:8790", ingestKey = null) {
    const url = new URL(endpoint);
    if (url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(url.hostname) || !url.port || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
      throw new Error("SDK endpoint must be an explicit-port loopback HTTP origin.");
    }
    this.port = Number(url.port);
    this.service = String(service).slice(0, 100);
    this.ingestKey = ingestKey;
    this.dropped = 0;
    this.lastError = null;
  }

  async event(message, level = "INFO", fields = {}) {
    try {
      const content = JSON.stringify({...fields, timestamp: new Date().toISOString(), service: this.service, level, message: String(message)});
      if (Buffer.byteLength(content) > 65536) throw new Error("SDK event exceeds 64 KiB.");
      const data = JSON.stringify({content});
      const headers = {"Content-Type": "application/json", "Content-Length": Buffer.byteLength(data)};
      if (this.ingestKey) headers.Authorization = "Bearer " + this.ingestKey;
      await new Promise((resolve, reject) => {
        const request = http.request({hostname: "127.0.0.1", port: this.port, path: "/api/ingest", method: "POST", headers}, response => {
          let body = "";
          response.setEncoding("utf8");
          response.on("data", chunk => { body += chunk; if (body.length > 4096) response.destroy(new Error("Oversized SDK response")); });
          response.on("error", reject);
          response.on("end", () => {
            try {
              if (response.statusCode !== 200 || JSON.parse(body).accepted !== 1) throw new Error("Event was not accepted.");
              resolve();
            } catch (error) { reject(error); }
          });
        });
        const deadline = setTimeout(() => request.destroy(new Error("Delivery timeout")), 1000);
        request.on("close", () => clearTimeout(deadline));
        request.on("error", reject);
        request.end(data);
      });
      this.lastError = null;
      return true;
    } catch (error) {
      this.lastError = error.name;
      this.dropped += 1;
      return false;
    }
  }

  async capture(action, fields = {}) {
    try { return await action(); }
    catch (error) {
      const message = error instanceof Error ? error.stack || error.message : String(error);
      await this.event(message, "ERROR", fields);
      throw error;
    }
  }
}
