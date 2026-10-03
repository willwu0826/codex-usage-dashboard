from __future__ import annotations
import argparse
import csv
import io
import json
import os
from pathlib import Path
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

sys.dont_write_bytecode = True
from usage_core import Ledger

ROOT = Path(__file__).resolve().parent.parent
WEB = Path(__file__).resolve().parent / 'web'
APP_ID = 'codex-project-usage-dashboard-v1'

def make_handler(ledger):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def reply(self, data, content_type='application/json; charset=utf-8', status=200, headers=None):
            if isinstance(data, (dict, list)):
                data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            elif isinstance(data, str):
                data = data.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def host_ok(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

        def do_GET(self):
            if not self.host_ok():
                return self.reply({'error': '只允许本机访问'}, status=403)
            target = urlsplit(self.path)
            try:
                if target.path == '/api/health':
                    return self.reply({'app': APP_ID, 'version': 2, 'root': str(ROOT), 'scanning': ledger.scanning, 'pid': os.getpid()})
                if target.path == '/api/dashboard':
                    return self.reply(ledger.dashboard())
                if target.path in ('/api/data', '/api/export.csv'):
                    report = ledger.report(parse_qs(target.query).get('period', ['week'])[0])
                    if target.path == '/api/data':
                        return self.reply(report)
                    output = io.StringIO(newline='')
                    writer = csv.writer(output)
                    writer.writerow(['项目', '统计范围', '输入token(含缓存)', '缓存输入token(输入子集)', '输出token(含推理)', '总token', '子Agent token(总量子集)', '会话数', '本机已记录token占比(非额度)', '日期未知token', '累计本地token', '归属依据'])
                    for p in report['projects']:
                        safe_name = "'" + p['name'] if p['name'].startswith(('=', '+', '-', '@')) else p['name']
                        writer.writerow([safe_name, report['period']['label'], p['input'], p['cached'], p['output'], p['total'], p['agent_tokens'], p['threads'], p['token_share'], p['undated_tokens'], report['lifetime'].get(p['id'], {}).get('total'), p['basis']])
                    return self.reply('\ufeff' + output.getvalue(), 'text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="codex-project-usage.csv"'})
                names = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
                if target.path in names:
                    filename, mime = names[target.path]
                    return self.reply((WEB / filename).read_bytes(), mime + '; charset=utf-8')
                self.reply({'error': '未找到'}, status=404)
            except ValueError as exc:
                self.reply({'error': str(exc)}, status=400)
            except Exception as exc:
                self.reply({'error': f'本地读取失败：{type(exc).__name__}: {exc}'}, status=500)

        def do_POST(self):
            allowed = (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')
            if not self.host_ok() or self.headers.get('X-Usage-Dashboard') != '1' or (self.headers.get('Origin') and self.headers['Origin'] not in allowed):
                return self.reply({'error': '请求来源不匹配'}, status=403)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 <= size <= 8192 or not self.headers.get('Content-Type', '').startswith('application/json'):
                    return self.reply({'error': '请求格式不支持'}, status=400)
                data = json.loads(self.rfile.read(size) or b'{}')
                if not isinstance(data, dict):
                    raise ValueError('请求内容必须是对象')
                if self.path == '/api/refresh':
                    ledger.start_refresh()
                    return self.reply({'scanning': ledger.scanning})
                if self.path == '/api/mapping':
                    ledger.set_mapping(data.get('thread_id'), data.get('project_id'))
                    return self.reply({'ok': True})
                if self.path == '/api/preferences':
                    ledger.set_preference(data.get('project_id'), data.get('focused'))
                    return self.reply({'ok': True})
                self.reply({'error': '未找到'}, status=404)
            except (ValueError, TypeError) as exc:
                self.reply({'error': str(exc)}, status=400)
    return Handler

def main():
    parser = argparse.ArgumentParser(description='本机 Codex 项目用量看板；配置默认读取仓库根目录 config.json。')
    parser.add_argument('--port', type=int, default=43129, help='本机服务端口，默认 43129')
    parser.add_argument('--scan-once', action='store_true', help='扫描一次并输出核验摘要，不启动网页服务')
    parser.add_argument('--workspace', help='项目工作区，覆盖 config.json 的 workspace')
    parser.add_argument('--codex-home', help='Codex 数据目录，覆盖 config.json 的 codex_home')
    parser.add_argument('--registry', help='可选会话与项目关联 JSON，覆盖 config.json 的 registry')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口必须在 1 到 65535 之间')
    try:
        ledger = Ledger(ROOT, codex_home=args.codex_home, workspace=args.workspace, registry=args.registry)
    except ValueError as exc:
        parser.error(str(exc))
    if args.scan_once:
        ledger.refresh()
        report = ledger.report('all')
        (ROOT / '03_数据' / '核验摘要.json').write_text(json.dumps({'scan': report['scan'], 'totals': report['totals'], 'quota': report['quota'], 'warnings': report['warnings']}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'scan': report['scan'], 'totals': report['totals'], 'error': ledger.last_failure}, ensure_ascii=False))
        return 1 if ledger.last_failure else 0
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(ledger))
    ledger.start_refresh()
    (ROOT / '03_数据' / '服务进程.json').write_text(json.dumps({'pid': os.getpid(), 'port': args.port, 'root': str(ROOT), 'app': APP_ID}, ensure_ascii=False), encoding='utf-8')
    print(f'http://127.0.0.1:{args.port}', flush=True)
    server.serve_forever()

if __name__ == '__main__':
    raise SystemExit(main())
