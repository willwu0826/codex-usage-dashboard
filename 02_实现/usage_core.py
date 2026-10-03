"""Local Codex usage ledger. No network, credentials or transcript text is read into the cache."""
from __future__ import annotations
import collections
from contextlib import closing
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import zlib

VERSION = 3
TZ = dt.timezone(dt.timedelta(hours=8))
FIELDS = ('input', 'cached', 'output', 'reasoning', 'total')
SOURCE_FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')

def stamp(value):
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0

def iso(value):
    return dt.datetime.fromtimestamp(value, TZ).isoformat(timespec='seconds') if value else None

def normalized(value):
    text = str(value or '').replace('/', '\\')
    # Some desktop DB records contain doubled separators, unlike native paths.
    while '\\\\' in text:
        text = text.replace('\\\\', '\\')
    if text.startswith('\\?\\'):
        text = text[3:]
    return text.rstrip('\\')

def project_id(path):
    return 'p:' + hashlib.sha256(normalized(path).casefold().encode()).hexdigest()[:16]

def numbers(usage):
    return {k: max(0, int(usage.get(s, 0) or 0)) for k, s in zip(FIELDS, SOURCE_FIELDS)}

def empty_totals():
    return {k: 0 for k in FIELDS}

def parse_rollout(path):
    """First session_meta owns file; inherited history older than owner creation is excluded."""
    result = {'meta': {}, 'events': [], 'quotas': [], 'errors': 0, 'duplicates': 0,
              'counter_resets': 0, 'unplaced_tokens': 0, 'partial_lines': 0, 'baselines': 0,
              'unsupported_usage': 0, 'ambiguous_counters': 0}
    previous = None
    model = '未知模型'
    owner = None
    cutoff = 0
    turn = None
    header_time = 0
    with Path(path).open('rb') as stream:
        for raw in stream:
            head = raw[:250]
            if not any(marker in head for marker in (b'"session_meta"', b'"turn_context"', b'"token_count"')):
                continue
            if not raw.endswith(b'\n'):
                result['partial_lines'] += 1
                continue  # writer can still be appending the last line
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeError):
                result['errors'] += 1
                continue
            payload = record.get('payload') or {}
            kind = record.get('type')
            if kind == 'session_meta':
                if owner is None:
                    owner = payload.get('id')
                    cutoff = stamp(payload.get('timestamp'))
                    header_time = stamp(record.get('timestamp'))
                    result['meta'] = {k: payload.get(k) for k in ('id', 'cwd', 'source', 'forked_from_id')}
                    result['meta']['created'] = cutoff
                continue
            if kind == 'turn_context':
                model = payload.get('model') or model
                turn = payload.get('turn_id') or turn
                continue
            if kind != 'event_msg' or payload.get('type') != 'token_count':
                continue
            when = stamp(record.get('timestamp'))
            if not owner or not when:
                result['errors'] += 1
                continue
            info = payload.get('info') or {}
            cumulative = info.get('total_token_usage')
            current = numbers(cumulative) if isinstance(cumulative, dict) else None
            last = numbers(info['last_token_usage']) if isinstance(info.get('last_token_usage'), dict) else None
            if when < cutoff:
                if current:
                    previous = current
                continue
            limits = payload.get('rate_limits') or {}
            for window in ('primary', 'secondary'):
                q = limits.get(window) or {}
                if q.get('window_minutes') == 10080 and isinstance(q.get('used_percent'), (int, float)) and q.get('resets_at'):
                    result['quotas'].append({'sampled': when, 'used_percent': q['used_percent'],
                                             'resets_at': q['resets_at'], 'window_minutes': 10080,
                                             'limit_id': limits.get('limit_id') or 'unknown'})
            if not current:
                if last and last['total']:
                    result['unsupported_usage'] += 1
                continue  # rate-only events are not another model call
            if previous and current['total'] == previous['total']:
                result['duplicates'] += 1
                continue
            if previous and current['total'] > previous['total'] and all(current[k] >= previous[k] for k in FIELDS):
                delta = {k: current[k] - previous[k] for k in FIELDS}
            elif previous:
                result['counter_resets'] += 1
                if last and current == last:
                    delta = last  # a verified fresh counter epoch
                else:
                    result['ambiguous_counters'] += 1
                    continue  # retain high-water mark; old/out-of-order balance is not spend
            elif last and current['total'] > last['total']:
                # A resumed/forked log may begin with a prior lifetime balance. Never charge it today.
                result['unplaced_tokens'] += current['total'] - last['total']
                delta = last
            else:
                delta = current
            previous = current
            if delta['total'] <= 0:
                continue
            if delta['input'] == 0 and delta['output'] == 0 and delta['total'] > 0:
                result['baselines'] += 1
                continue  # legacy restoration token balance, not a new inference
            if delta['cached'] > delta['input'] or delta['reasoning'] > delta['output'] or delta['total'] != delta['input'] + delta['output']:
                result['errors'] += 1
                continue
            identity = json.dumps([owner, when, current], sort_keys=True)
            result['events'].append({'key': hashlib.sha256(identity.encode()).hexdigest(), 'thread': owner,
                                     'when': when, 'model': model, 'turn': turn,
                                     'fingerprint': hashlib.sha256(json.dumps([current, last, turn], sort_keys=True).encode()).hexdigest(),
                                     'time_unknown': False, **delta})
    # Desktop migrations can stamp an entire historic prefix with the header time.
    # Such dates are not evidence of when the work happened.
    matching = [e for e in result['events'] if e['when'] == header_time]
    if len(matching) > 1 or (result['baselines'] and matching):
        for event in matching:
            event['time_unknown'] = True
        result['quotas'] = [q for q in result['quotas'] if q['sampled'] != header_time]
    return result

class Ledger:
    def __init__(self, root, codex_home=None, workspace=None, registry=None, projects=None):
        self.root = Path(root).resolve()
        config_path = self.root / 'config.json'
        try:
            config = self.read_json(config_path, {})
        except (ValueError, UnicodeError) as exc:
            raise ValueError('config.json 不是有效的 UTF-8 JSON 文件') from exc
        if not isinstance(config, dict):
            raise ValueError('config.json 顶层必须是 JSON 对象')
        unknown = set(config) - {'codex_home', 'workspace', 'registry', 'projects'}
        if unknown:
            raise ValueError('config.json 包含不支持的字段：' + '、'.join(sorted(unknown)))

        def path_value(key, override, fallback=None):
            value = override if override is not None else config.get(key)
            if value is not None and not isinstance(value, (str, os.PathLike)):
                raise ValueError(f'配置 {key} 必须是路径字符串')
            value = value or fallback
            if value is None:
                return None
            path = Path(os.path.expandvars(str(value))).expanduser()
            return (path if path.is_absolute() else self.root / path).resolve()

        self.codex_home = path_value('codex_home', codex_home, os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        self.workspace = path_value('workspace', workspace, self.root.parent)
        self.registry = path_value('registry', registry)
        project_paths = projects if projects is not None else config.get('projects', [])
        if not isinstance(project_paths, list) or any(not isinstance(p, str) or not p.strip() for p in project_paths):
            raise ValueError('配置 projects 必须是非空路径字符串组成的数组')
        self.project_paths = [path_value('projects', path) for path in project_paths]
        self.data_dir = self.root / '03_数据'
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.scanning = False
        self.last_failure = None
        self.snapshot = None
        self.mapping_path = self.data_dir / '项目归属.json'
        self.manual = self.read_json(self.mapping_path, {})
        self.preferences_path = self.data_dir / '看板偏好.json'
        self.preferences = self.read_json(self.preferences_path, {'focused_projects': []})

    @staticmethod
    def read_json(path, fallback):
        try:
            return json.loads(Path(path).read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return fallback

    def metadata(self):
        db = self.codex_home / 'state_5.sqlite'
        threads, parents = {}, {}
        if db.exists():
            with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                columns = {r[1] for r in conn.execute('pragma table_info(threads)')}
                selected = ['id', 'rollout_path', 'cwd', 'source', 'model', 'created_at', 'updated_at']
                selected = [c for c in selected if c in columns]
                query = 'select ' + ','.join(selected) + ',substr(title,1,140) title'
                query += ',substr(name,1,140) name' if 'name' in columns else ''
                for row in conn.execute(query + ' from threads'):
                    item = dict(row)
                    item['title'] = (item.get('name') or item.get('title') or item['id']).splitlines()[0][:100]
                    item.pop('name', None)
                    threads[item['id']] = item
                    try:
                        source = json.loads(item.get('source') or '{}')
                        parent = source.get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id')
                        if parent:
                            parents[item['id']] = parent
                    except (ValueError, AttributeError):
                        pass
                if conn.execute("select 1 from sqlite_master where name='thread_spawn_edges'").fetchone():
                    for parent, child in conn.execute('select parent_thread_id,child_thread_id from thread_spawn_edges'):
                        parents[child] = parent
        return threads, parents

    def catalog(self):
        catalog, links = {}, collections.defaultdict(set)
        def add(path):
            path = normalized(path)
            if path:
                pid = project_id(path)
                catalog[pid] = {'id': pid, 'name': path.split('\\')[-1], 'path': path}
                return pid
        if self.workspace.is_dir():
            # Only the workspace and one category level; never walk the whole drive.
            for path in self.workspace.glob('project_*'):
                if path.is_dir():
                    add(path)
            for directory in self.workspace.iterdir():
                if directory.is_dir() and not directory.name.startswith('.'):
                    for path in directory.glob('project_*'):
                        if path.is_dir():
                            add(path)
        for path in self.project_paths:
            add(path)
        registry = self.read_json(self.registry, {}) if self.registry else {}
        for row in registry.get('entries', []):
            if row.get('threadId') and row.get('projectRoot'):
                links[row['threadId']].add(add(row['projectRoot']))
        for path in self.manual.values():
            add(path)
        return catalog, dict(links)

    def start_refresh(self):
        with self.lock:
            if self.scanning:
                return False
            self.scanning = True
        threading.Thread(target=self.refresh, daemon=True, name='usage-ledger-scan').start()
        return True

    def refresh(self):
        started = time.time()
        try:
            threads, parents = self.metadata()
            paths = set()
            for folder in ('sessions', 'archived_sessions'):
                base = self.codex_home / folder
                if base.is_dir():
                    paths.update(p.resolve() for p in base.rglob('*.jsonl'))
            missing = 0
            for row in threads.values():
                p = Path(normalized(row.get('rollout_path')))
                if not p.is_file():
                    missing += 1
            events, quotas, forks = {}, [], {}
            stats = dict(files=0, events=0, duplicates=0, errors=0, missing_files=missing,
                         zero_usage_threads=0, counter_resets=0, unplaced_tokens=0, partial_lines=0,
                         inherited_copies=0, last_scan=None, notes=[], cached_files=0, baselines=0,
                         undated_tokens=0, undated_events=0, unsupported_usage=0, ambiguous_counters=0)
            with closing(sqlite3.connect(self.data_dir / '用量缓存.sqlite')) as cache:
                cache.execute('create table if not exists parsed(path text primary key, size integer, mtime integer, version integer, payload blob)')
                for p in sorted(paths):
                    stat = p.stat()
                    previous = cache.execute('select size,mtime,version,payload from parsed where path=?', (str(p),)).fetchone()
                    if previous and previous[:3] == (stat.st_size, stat.st_mtime_ns, VERSION):
                        parsed = json.loads(zlib.decompress(previous[3]))
                        stats['cached_files'] += 1
                    else:
                        parsed = parse_rollout(p)
                        packed = zlib.compress(json.dumps(parsed, ensure_ascii=False).encode('utf-8'))
                        cache.execute('insert or replace into parsed values(?,?,?,?,?)', (str(p), stat.st_size, stat.st_mtime_ns, VERSION, packed))
                    stats['files'] += 1
                    meta = parsed['meta']
                    tid = meta.get('id')
                    if tid:
                        if meta.get('forked_from_id'):
                            forks[tid] = meta['forked_from_id']
                        row = threads.setdefault(tid, {'id': tid, 'title': tid, 'cwd': meta.get('cwd'), 'model': '未知模型'})
                        row.setdefault('cwd', meta.get('cwd'))
                        source = meta.get('source')
                        if isinstance(source, dict):
                            parent = source.get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id')
                            if parent:
                                parents[tid] = parent
                    for key in ('duplicates', 'errors', 'counter_resets', 'unplaced_tokens', 'partial_lines', 'baselines', 'unsupported_usage', 'ambiguous_counters'):
                        stats[key] += parsed[key]
                    for event in parsed['events']:
                        if event['key'] in events:
                            stats['inherited_copies'] += 1
                        else:
                            events[event['key']] = event
                    quotas.extend(parsed['quotas'])
                cache.commit()
            ancestry = {**forks, **parents}
            # Copied historical token events may have rewritten timestamps. Match their
            # original counters and turn IDs within a known ancestry, never across unrelated chats.
            canonical = {}
            def depth(tid):
                seen = set()
                while tid in ancestry and tid not in seen:
                    seen.add(tid)
                    tid = ancestry[tid]
                return len(seen)
            for event in sorted(events.values(), key=lambda e: (depth(e['thread']), e['time_unknown'], e['when'])):
                key = (self.root_thread(event['thread'], ancestry), event['fingerprint'])
                if key in canonical:
                    original = canonical[key]
                    cursor, ancestors = event['thread'], set()
                    while cursor in ancestry and cursor not in ancestors:
                        ancestors.add(cursor)
                        cursor = ancestry[cursor]
                    ancestors.add(cursor)
                    if event['turn'] or original['thread'] in ancestors:
                        stats['inherited_copies'] += 1
                        continue
                    # Equal numbers in unrelated sibling calls are not proof of a copy.
                    key = (*key, event['thread'])
                canonical[key] = event
            events = canonical
            stats['events'] = len(events)
            stats['undated_tokens'] = sum(e['total'] for e in events.values() if e['time_unknown'])
            stats['undated_events'] = sum(e['time_unknown'] for e in events.values())
            with_usage = {e['thread'] for e in events.values()}
            stats['zero_usage_threads'] = len(set(threads) - with_usage)
            stats['last_scan'] = iso(time.time())
            stats['seconds'] = round(time.time() - started, 2)
            with self.lock:
                self.snapshot = {'threads': threads, 'parents': parents, 'events': list(events.values()),
                                 'quotas': quotas, 'scan': stats}
                self.last_failure = None
        except Exception as exc:
            with self.lock:
                self.last_failure = f'{type(exc).__name__}: {exc}'
        finally:
            with self.lock:
                self.scanning = False

    @staticmethod
    def root_thread(tid, parents):
        seen = set()
        while tid in parents and tid not in seen:
            seen.add(tid)
            tid = parents[tid]
        return tid

    def attribution(self, tid, threads, parents, catalog, links):
        root = self.root_thread(tid, parents)
        if root in self.manual:
            return project_id(self.manual[root]), '手工指定会话', root
        candidates = links.get(root, set())
        if len(candidates) == 1:
            return next(iter(candidates)), '已有会话登记', root
        if len(candidates) > 1:
            return 'unassigned', '同会话关联多个项目', root
        cwd = normalized(threads.get(root, {}).get('cwd')).casefold()
        choices = [p for p in catalog.values() if cwd == p['path'].casefold() or cwd.startswith(p['path'].casefold() + '\\')]
        if choices:
            best = max(choices, key=lambda p: len(p['path']))
            return best['id'], '项目工作目录', root
        return 'unassigned', '缺少明确项目对应关系', root

    def set_mapping(self, tid, pid):
        with self.lock:
            if not self.snapshot or tid not in self.snapshot['threads']:
                raise ValueError('未找到该会话')
            if tid in self.snapshot['parents']:
                raise ValueError('子 Agent 跟随主会话，请修改主会话归属')
            catalog, _ = self.catalog()
            if pid and pid not in catalog:
                raise ValueError('未找到该项目')
            new = dict(self.manual)
            if pid:
                new[tid] = catalog[pid]['path']
            else:
                new.pop(tid, None)
            tmp = self.mapping_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(tmp, self.mapping_path)
            self.manual = new

    def report(self, period='week', now=None):
        now = now or time.time()
        with self.lock:
            snapshot = self.snapshot or {'threads': {}, 'parents': {}, 'events': [], 'quotas': [], 'scan': {}}
            scanning = self.scanning
            failure = self.last_failure
            catalog, links = self.catalog()
            manual = dict(self.manual)
            preferences = {'focused_projects': list(self.preferences.get('focused_projects', []))}
        warnings = ['仅覆盖这台电脑保留的 Codex 日志，不含没有同步到本机的云端或其他设备用量。',
                    '项目归集依据会话登记或目录；同一会话历史上切换项目时，整会话归属可能需要人工核对。',
                    '账号周额度是日志中的最近快照；刷新看板不会向 OpenAI 发起额度查询。']
        if failure:
            warnings.append('刷新失败，保留上次结果：' + failure)
        quota = None
        candidates = [q for q in snapshot['quotas'] if q['limit_id'] == 'codex'
                      and isinstance(q.get('resets_at'), (int, float))
                      and math.isfinite(q['resets_at'])
                      and isinstance(q.get('used_percent'), (int, float))
                      and math.isfinite(q['used_percent']) and 0 <= q['used_percent'] <= 100
                      and q['resets_at'] - 604800 <= q['sampled'] < q['resets_at']
                      and q['sampled'] <= now]
        if candidates:
            q = max(candidates, key=lambda q: q['sampled'])
            expired = q['resets_at'] <= now
            quota = {**q, 'sampled_at': iso(q['sampled']), 'resets_at': iso(q['resets_at']),
                     'window_start': iso(q['resets_at'] - 604800),
                     'remaining_percent': max(0, 100-q['used_percent']) if not expired else None,
                     'stale': now - q['sampled'] > 3600 or expired, 'expired': expired}
        start = None
        labels = {'week': '当前额度周', 'today': '今天', '7d': '近 7 天', '30d': '近 30 天', 'all': '全部本地记录'}
        if period not in labels:
            raise ValueError('不支持的时间范围')
        if period == 'week':
            if quota and not quota['expired']:
                start = stamp(quota['window_start'])
            else:
                start = now + 1
                warnings.append('当前没有有效周窗口快照。请切换“近 7 天”或“全部本地记录”，不可把自然周冒充额度周。')
        elif period == 'today':
            start = dt.datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        elif period in ('7d', '30d'):
            days = int(period[:-1])
            start = (dt.datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=days-1)).timestamp()
        totals = {**empty_totals(), 'agent_tokens': 0, 'threads': 0, 'mapped_tokens': 0, 'unmapped_tokens': 0, 'undated_tokens': 0}
        projects, by_thread, daily, models = {}, {}, {}, collections.Counter()
        choices = dict(catalog)
        choices['unassigned'] = {'id': 'unassigned', 'name': '未归属 / 共享工作', 'path': ''}
        assignments = {}
        for tid in snapshot['threads']:
            assignments[tid] = self.attribution(tid, snapshot['threads'], snapshot['parents'], catalog, links)
        lifetime = {}
        for event in snapshot['events']:
            if event['when'] > now:
                continue
            pid = assignments.get(event['thread'], ('unassigned',))[0]
            item = lifetime.setdefault(pid, {**empty_totals(), 'undated_tokens': 0})
            for k in FIELDS:
                item[k] += event[k]
            if event.get('time_unknown'):
                item['undated_tokens'] += event['total']
        for event in snapshot['events']:
            if event.get('time_unknown') and period != 'all':
                continue
            if event['when'] > now or (start is not None and event['when'] < start):
                continue
            tid = event['thread']
            pid, basis, root_tid = assignments.get(tid, ('unassigned', '元数据缺失', tid))
            is_agent = tid in snapshot['parents']
            p = projects.setdefault(pid, {**choices.get(pid, choices['unassigned']), **empty_totals(),
                                         'agent_tokens': 0, 'undated_tokens': 0, '_threads': set(), '_basis': set(),
                                         '_models': collections.Counter(), '_daily': {}})
            item = snapshot['threads'].get(tid, {})
            row = by_thread.setdefault(tid, {'id': tid, 'title': item.get('title') or tid, 'project_id': pid,
                                            'project_name': p['name'], 'basis': ('继承主会话 · ' if is_agent else '')+basis,
                                            'root_thread_id': root_tid, 'is_agent': is_agent,
                                            'manual': root_tid in manual, **empty_totals(), 'model': event['model'], 'updated_at': None,
                                            'agent_tokens': 0, 'including_agents_total': 0, 'undated_tokens': 0, '_models': set()})
            for k in FIELDS:
                totals[k] += event[k]
                p[k] += event[k]
                row[k] += event[k]
            row['_models'].add(event['model'])
            p['_models'][event['model']] += event['total']
            if event.get('time_unknown'):
                for item in (totals, p, row):
                    item['undated_tokens'] += event['total']
            row['updated_at'] = max(row['updated_at'] or '', iso(event['when']))
            p['_threads'].add(tid)
            p['_basis'].add(basis)
            if is_agent:
                totals['agent_tokens'] += event['total']
                p['agent_tokens'] += event['total']
            totals['unmapped_tokens' if pid == 'unassigned' else 'mapped_tokens'] += event['total']
            if not event.get('time_unknown'):
                day = dt.datetime.fromtimestamp(event['when'], TZ).date().isoformat()
                daily_row = daily.setdefault(day, {'date': day, 'total': 0, 'cached': 0, 'output': 0})
                for k in ('total', 'cached', 'output'):
                    daily_row[k] += event[k]
                project_day = p['_daily'].setdefault(day, {'date': day, 'total': 0, 'cached': 0, 'output': 0})
                for k in ('total', 'cached', 'output'):
                    project_day[k] += event[k]
            models[event['model']] += event['total']
        for tid, row in list(by_thread.items()):
            if row['is_agent']:
                parent = row['root_thread_id']
                if parent not in by_thread:
                    item = snapshot['threads'].get(parent, {})
                    by_thread[parent] = {**row, **empty_totals(), 'id': parent, 'title': item.get('title') or parent,
                                         'root_thread_id': parent, 'is_agent': False, 'basis': row['basis'].replace('继承主会话 · ', ''),
                                         'agent_tokens': 0, 'including_agents_total': 0, 'undated_tokens': 0, '_models': set()}
                by_thread[parent]['agent_tokens'] += row['total']
        for row in by_thread.values():
            row['including_agents_total'] = row['total'] + row['agent_tokens']
            row['model'] = '、'.join(sorted(row.pop('_models'))) or '仅子 Agent 有记录'
        for p in projects.values():
            p['threads'] = len(p.pop('_threads'))
            p['basis'] = '、'.join(sorted(p.pop('_basis')))
            p['token_share'] = p['total']/totals['total'] if totals['total'] else 0
            p['quota_estimate_pp'] = None  # Deprecated: raw token share is not subscription quota attribution.
            p['models'] = [{'name': name, 'total': total} for name, total in p.pop('_models').most_common()]
            p['daily'] = [value for _, value in sorted(p.pop('_daily').items())]
        totals['threads'] = len(by_thread)
        stats = {**snapshot['scan'], 'scanning': scanning, 'failure': failure}
        if stats.get('unplaced_tokens'):
            warnings.append('部分首条快照含继承或恢复的历史累计余额；已排除余额，避免把之前的消耗重复计入当前会话。')
        if stats.get('undated_tokens'):
            warnings.append(f"迁移记录中 {stats['undated_tokens']:,} token 缺少可信日期；仅纳入全部记录，不进入本周、日趋势等时间统计。")
        if stats.get('counter_resets'):
            warnings.append(f"检测到 {stats['counter_resets']} 次计数回退；仅接纳可验证的重新计数，其余 {stats.get('ambiguous_counters', 0)} 条隔离。总账是本地观测值，非官方账单。")
        if stats.get('unsupported_usage'):
            warnings.append(f"另有 {stats['unsupported_usage']} 条用量缺少累计计数，无法安全去重，未计入总账。")
        if stats.get('missing_files') or stats.get('errors'):
            warnings.append('部分源文件缺失或用量格式异常，详见覆盖情况；缺失不等于零消耗。')
        return {'generated_at': iso(now), 'scan': stats, 'quota': quota,
                'period': {'id': period, 'label': labels[period], 'start': iso(start) if start and start <= now else None, 'end': iso(now)},
                'totals': totals, 'projects': sorted(projects.values(), key=lambda p: -p['total']),
                'daily': [daily[d] for d in sorted(daily)], 'models': [{'name': k, 'total': v} for k,v in models.most_common()],
                'threads': sorted(by_thread.values(), key=lambda p: -p['including_agents_total']),
                'project_options': sorted(catalog.values(), key=lambda p: p['name']), 'warnings': warnings,
                'lifetime': lifetime, 'preferences': preferences}

    def dashboard(self):
        # One lock and clock: both views use the same scan and manual assignments.
        with self.lock:
            now = time.time()
            return {'week': self.report('week', now), 'all': self.report('all', now)}

    def set_preference(self, pid, focused):
        if type(focused) is not bool:
            raise ValueError('关注状态必须是布尔值')
        with self.lock:
            catalog, _ = self.catalog()
            if not isinstance(pid, str) or pid not in catalog:
                raise ValueError('未找到该项目')
            chosen = set(self.preferences.get('focused_projects', []))
            if focused:
                chosen.add(pid)
            else:
                chosen.discard(pid)
            new = {'focused_projects': sorted(chosen)}
            tmp = self.preferences_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(tmp, self.preferences_path)
            self.preferences = new
