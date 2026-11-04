"""拉某个 run 的失败 job 日志，只打印关键行。"""
import os
import re
import subprocess

import httpx

TOKEN = os.environ["GH_TOKEN"]
RUN = os.environ.get("RUN_ID", "")
PROXY = "http://127.0.0.1:7897"

with httpx.Client(trust_env=False, proxy=PROXY, timeout=60) as c:
    h = {"Authorization": f"token {TOKEN}", "User-Agent": "ai-study",
         "Accept": "application/vnd.github+json"}
    runs = c.get("https://api.github.com/repos/zbq133/AI-study/actions/runs?per_page=3", headers=h).json()
    run = next((r for r in runs["workflow_runs"] if not RUN or r["id"] == int(RUN)), runs["workflow_runs"][0])
    print("run", run["id"], run["head_sha"][:7], run["conclusion"])
    jobs = c.get(f"https://api.github.com/repos/zbq133/AI-study/actions/runs/{run['id']}/jobs", headers=h).json()
    for j in jobs["jobs"]:
        print("JOB", j["name"], j["conclusion"])
        for s in j["steps"]:
            print("   -", s["name"], s["conclusion"])
        if j["conclusion"] == "failure":
            # 日志走 302 到 blob，带 Authorization 会被拒；先取 Location 再裸下
            url = c.get(f"https://api.github.com/repos/zbq133/AI-study/actions/jobs/{j['id']}/logs",
                        headers=h, follow_redirects=False).headers.get("location")
            subprocess.run(["curl.exe", "-sSL", "--ssl-no-revoke", "--proxy", PROXY, url,
                            "-o", r"C:\Users\13304\AppData\Local\Temp\jlog.txt"], check=False)
            text = open(r"C:\Users\13304\AppData\Local\Temp\jlog.txt", encoding="utf-8", errors="replace").read()
            keep = [ln for ln in text.splitlines()
                    if re.search(r"FAILED|^E  |assert |ERROR|No matching", ln)]
            print("\n".join(ln[-160:] for ln in keep[:25]))
