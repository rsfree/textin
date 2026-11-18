#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TextIn headless 适配器实时看板

读取适配器输出目录里的 results.jsonl / failed.jsonl，起一个本地 HTTP 服务，
浏览器打开 http://127.0.0.1:8765 即可实时查看：成功/失败、错误码分布、
账号/IP 命中率、每分钟吞吐、最近记录。

用法：
    python dashboard.py                     # 默认看 ./out
    python dashboard.py --data-dir /tmp/bout --port 8765
    python dashboard.py --once              # 只打印汇总，不起服务
"""
from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TextIn 去水印 · 运行看板</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#ffffff; --line:#e6e8ec; --txt:#1a1d23; --sub:#6b7280;
    --ok:#16a34a; --bad:#dc2626; --warn:#d97706; --accent:#2563eb;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}
  .wrap{max-width:1180px;margin:0 auto;padding:24px 20px 60px}
  header{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:18px}
  h1{font-size:19px;margin:0;font-weight:600;letter-spacing:.2px}
  .meta{color:var(--sub);font-size:12px}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
  .card .k{color:var(--sub);font-size:12px;margin-bottom:6px}
  .card .v{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}
  .card .s{color:var(--sub);font-size:12px;margin-top:2px}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px}
  .panel h2{font-size:13px;margin:0 0 12px;font-weight:600;color:var(--sub);letter-spacing:.4px}
  .two{display:grid;grid-template-columns:1.1fr .9fr;gap:14px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
  th{color:var(--sub);font-weight:500;font-size:12px}
  td.mono{font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .pill{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;font-weight:600}
  .pill.ok{background:#dcfce7;color:#166534}
  .pill.bad{background:#fee2e2;color:#991b1b}
  .pill.warn{background:#fef3c7;color:#92400e}
  .bar{height:8px;border-radius:4px;background:var(--line);overflow:hidden}
  .bar i{display:block;height:100%;background:var(--accent)}
  .codebar{display:flex;align-items:center;gap:10px;margin:6px 0}
  .codebar .lb{width:70px;font-size:12px;color:var(--sub)}
  .codebar .track{flex:1;height:12px;border-radius:6px;background:var(--line);overflow:hidden}
  .codebar .fill{height:100%}
  .empty{color:var(--sub);font-size:13px;padding:8px 0}
  .live{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--ok);margin-right:6px;
        animation:pulse 1.6s infinite}
  @keyframes pulse{50%{opacity:.35}}
  .scroll{max-height:300px;overflow:auto}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>TextIn 去水印 · 运行看板</h1>
    <div class="meta"><span class="live"></span><span id="upd">加载中…</span></div>
  </header>

  <div class="cards">
    <div class="card"><div class="k">任务总数</div><div class="v" id="c_total">-</div><div class="s" id="c_files">-</div></div>
    <div class="card"><div class="k">成功</div><div class="v" id="c_ok" style="color:var(--ok)">-</div><div class="s" id="c_rate">-</div></div>
    <div class="card"><div class="k">失败</div><div class="v" id="c_fail" style="color:var(--bad)">-</div><div class="s" id="c_failnote">-</div></div>
    <div class="card"><div class="k">平均耗时</div><div class="v" id="c_ms">-</div><div class="s" id="c_p95">-</div></div>
  </div>

  <div class="two">
    <div class="panel">
      <h2>每分钟吞吐（绿=成功 / 红=被拒）</h2>
      <div id="timeline"></div>
    </div>
    <div class="panel">
      <h2>响应码分布</h2>
      <div id="codes"></div>
    </div>
  </div>

  <div class="panel">
    <h2>账号 / 出口 IP 命中率</h2>
    <div class="scroll"><table id="accts">
      <thead><tr><th>账号</th><th>出口 IP</th><th>成功</th><th>被拒</th><th>命中率</th><th>最后活动</th></tr></thead>
      <tbody></tbody>
    </table></div>
  </div>

  <div class="panel">
    <h2>最近记录</h2>
    <div class="scroll"><table id="recent">
      <thead><tr><th>时间</th><th>文件</th><th>账号</th><th>IP</th><th>发次</th><th>码</th><th>耗时</th></tr></thead>
      <tbody></tbody>
    </table></div>
  </div>
</div>
<script>
const fmt = n => (n === undefined || n === null) ? '-' : n;
async function tick(){
  try{
    const r = await fetch('/api/stats', {cache:'no-store'});
    const d = await r.json();
    document.getElementById('upd').textContent =
      '实时 · 更新于 ' + d.now + ' · 目录 ' + d.dir;
    document.getElementById('c_total').textContent = d.summary.tasks;
    document.getElementById('c_files').textContent =
      d.summary.ok + ' 已出图 / ' + d.summary.failed + ' 放弃';
    document.getElementById('c_ok').textContent = d.summary.ok;
    document.getElementById('c_rate').textContent = d.summary.success_rate + '% 成功率';
    document.getElementById('c_fail').textContent = d.summary.failed;
    document.getElementById('c_failnote').textContent =
      '共 ' + d.summary.rejected + ' 次被拒请求';
    document.getElementById('c_ms').textContent = d.summary.avg_ms ? d.summary.avg_ms + ' ms' : '-';
    document.getElementById('c_p95').textContent = d.summary.p95_ms ? 'P95 ' + d.summary.p95_ms + ' ms' : '-';

    // codes
    const codes = document.getElementById('codes');
    if(!d.codes.length){ codes.innerHTML = '<div class="empty">暂无数据</div>'; }
    else{
      const max = Math.max(...d.codes.map(x=>x.n));
      const color = c => c==200 ? 'var(--ok)' : c==431 ? 'var(--warn)' : 'var(--bad)';
      codes.innerHTML = d.codes.map(x =>
        `<div class="codebar"><div class="lb">${x.code==200?'200 成功':x.code+' '+(x.label||'')}</div>
         <div class="track"><div class="fill" style="width:${(x.n/max*100).toFixed(1)}%;background:${color(x.code)}"></div></div>
         <div style="width:44px;text-align:right;color:var(--sub)">${x.n}</div></div>`).join('');
    }

    // timeline (SVG bars)
    const tl = document.getElementById('timeline');
    if(!d.timeline.length){ tl.innerHTML = '<div class="empty">暂无数据</div>'; }
    else{
      const W=560,H=170,P=26, n=d.timeline.length;
      const max=v=>Math.max(1,...d.timeline.map(t=>t.ok+t.bad));
      const mx=max();
      const bw=Math.min(28,(W-P*2)/n*0.7);
      let svg=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}">`;
      svg+=`<line x1="${P}" y1="${H-24}" x2="${W-P}" y2="${H-24}" stroke="var(--line)"/>`;
      d.timeline.forEach((t,i)=>{
        const x=P+i*((W-P*2)/n);
        const tot=t.ok+t.bad;
        const hAll=(H-70)*tot/mx;
        const hOk=(H-70)*t.ok/mx;
        svg+=`<rect x="${x}" y="${H-24-hAll}" width="${bw}" height="${hAll}" fill="var(--bad)" rx="2" opacity=".85"/>`;
        svg+=`<rect x="${x}" y="${H-24-hOk}" width="${bw}" height="${hOk}" fill="var(--ok)" rx="2"/>`;
        if(n<=14 || i%2===0) svg+=`<text x="${x+bw/2}" y="${H-8}" font-size="9" fill="var(--sub)" text-anchor="middle">${t.t}</text>`;
      });
      svg+=`</svg>`;
      tl.innerHTML=svg;
    }

    // accounts
    const at=document.querySelector('#accts tbody');
    at.innerHTML = d.accounts.length ? d.accounts.map(a=>{
      const rate = a.ok+a.bad ? Math.round(a.ok/(a.ok+a.bad)*100) : 0;
      const pill = rate>=60?'ok':rate>0?'warn':'bad';
      return `<tr><td class="mono">${a.acct||'-'}</td><td class="mono">${a.ip||'-'}</td>
        <td class="mono">${a.ok}</td><td class="mono">${a.bad}</td>
        <td><span class="pill ${pill}">${rate}%</span></td><td class="mono">${a.last}</td></tr>`;
    }).join('') : '<tr><td colspan="6" class="empty">暂无数据</td></tr>';

    // recent
    const rt=document.querySelector('#recent tbody');
    rt.innerHTML = d.recent.length ? d.recent.map(r=>{
      const cls = r.code==200?'ok':r.code==431?'warn':'bad';
      return `<tr><td class="mono">${r.at}</td><td>${(r.file||'').split('/').pop()}</td>
        <td class="mono">${r.acct||'-'}</td><td class="mono">${r.ip||'-'}</td>
        <td class="mono">#${r.attempt||1}/${r.shot||1}</td>
        <td><span class="pill ${cls}">${r.code}</span></td>
        <td class="mono">${r.ms?r.ms+'ms':'-'}</td></tr>`;
    }).join('') : '<tr><td colspan="7" class="empty">暂无数据</td></tr>';
  }catch(e){
    document.getElementById('upd').textContent = '连接失败：' + e.message;
  }
}
tick();
setInterval(tick, 3000);
</script>
</body>
</html>
"""

CODE_LABELS = {200: "成功", 431: "今日额度", 441: "限流", -1: "解析失败"}


def load(data_dir: Path) -> dict:
    rows: list[dict] = []
    for name in ("results.jsonl", "failed.jsonl"):
        p = data_dir / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    ok_rows = [r for r in rows if r.get("code") == 200]
    rejected = [r for r in rows if r.get("code") not in (200, None)]
    failed_rows = [r for r in rows if "reason" in r]

    # per-file tasks
    files_ok = {r.get("file") for r in ok_rows}
    tasks = len(files_ok) + len(failed_rows)

    lat = [r["ms"] for r in ok_rows if isinstance(r.get("ms"), (int, float)) and r["ms"] > 0]
    avg_ms = int(statistics.mean(lat)) if lat else 0
    p95 = int(sorted(lat)[int(len(lat) * 0.95) - 1]) if len(lat) > 4 else (max(lat) if lat else 0)

    codes = Counter(r.get("code") for r in rows if r.get("code") is not None)
    code_list = [
        {"code": c, "n": n, "label": CODE_LABELS.get(c, "")}
        for c, n in codes.most_common()
    ]

    # per account/ip
    acc: dict[str, dict] = defaultdict(lambda: {"ok": 0, "bad": 0, "last": "", "ip": ""})
    for r in rows:
        key = r.get("acct") or (r.get("ip") or "direct")
        a = acc[key]
        a["ip"] = r.get("ip") or a["ip"]
        if r.get("code") == 200:
            a["ok"] += 1
        elif r.get("code") is not None:
            a["bad"] += 1
        a["last"] = max(a["last"], r.get("at") or "")
    accounts = [
        {"acct": k, "ip": v["ip"], "ok": v["ok"], "bad": v["bad"], "last": v["last"]}
        for k, v in acc.items()
    ]
    accounts.sort(key=lambda x: (-x["ok"], x["acct"]))

    # per-minute timeline
    tl: dict[str, dict] = defaultdict(lambda: {"ok": 0, "bad": 0})
    for r in rows:
        at = r.get("at") or ""
        if len(at) < 5:
            continue
        minute = at[:5]
        if r.get("code") == 200:
            tl[minute]["ok"] += 1
        elif r.get("code") is not None:
            tl[minute]["bad"] += 1
    timeline = [{"t": k, "ok": v["ok"], "bad": v["bad"]} for k, v in sorted(tl.items())]

    recent = sorted(rows, key=lambda r: r.get("at") or "")[-25:][::-1]

    return {
        "now": time.strftime("%H:%M:%S"),
        "dir": str(data_dir.resolve()),
        "summary": {
            "tasks": tasks,
            "ok": len(files_ok),
            "failed": len(failed_rows),
            "rejected": len(rejected),
            "success_rate": round(len(files_ok) / tasks * 100) if tasks else 0,
            "avg_ms": avg_ms,
            "p95_ms": p95,
        },
        "codes": code_list,
        "accounts": accounts[:40],
        "timeline": timeline[-40:],
        "recent": recent,
    }


def make_handler(data_dir: Path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: ANN002
            pass

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/stats"):
                payload = json.dumps(load(data_dir), ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return H


def main() -> int:
    ap = argparse.ArgumentParser(description="TextIn headless 适配器实时看板")
    ap.add_argument("--data-dir", default="out", help="适配器输出目录（含 results.jsonl）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--once", action="store_true", help="只打印一次汇总")
    args = ap.parse_args()

    d = Path(args.data_dir)
    if args.once:
        s = load(d)
        print(json.dumps(s["summary"], ensure_ascii=False, indent=1))
        print("codes:", s["codes"])
        print("accounts:", len(s["accounts"]), "timeline_minutes:", len(s["timeline"]))
        return 0

    srv = ThreadingHTTPServer((args.host, args.port), make_handler(d))
    print(f"看板: http://{args.host}:{args.port}  (data={d.resolve()})  Ctrl-C 停止", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
