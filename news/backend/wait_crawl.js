const http = require("http");

function check() {
  http.get("http://127.0.0.1:3001/api/crawl/status", (res) => {
    let data = "";
    res.on("data", (c) => (data += c));
    res.on("end", () => {
      try {
        const d = JSON.parse(data);
        const latest = d.latest || {};
        if (!d.running && latest.status === "completed") {
          console.log("Crawl completed!");
          const { execSync } = require("child_process");
          execSync(
            `osascript -e 'display notification "爬虫已完成！共 ${latest.success} 只股票" with title "因子系统"'`
          );
          process.exit(0);
        } else {
          console.log(
            `Progress: ${latest.success || 0}/${latest.total || 0}  (status=${latest.status})`
          );
        }
      } catch (e) {
        console.error("Parse error:", e.message);
      }
    });
  }).on("error", (e) => {
    console.error("Request error:", e.message);
  });
}

check();
setInterval(check, 30000);
