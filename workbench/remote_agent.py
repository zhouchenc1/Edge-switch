"""Uploaded over SSH stdin; Python 3.8+, no third-party dependencies."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse


PROXIES = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')


TOOL_PACKAGES = {
    'codex': ('@openai/codex', 'codex'),
    'harness': ('@deepseek-ai/dsh', 'dsh'),
    'claude': ('@anthropic-ai/claude-code', 'claude'),
    'gemini': ('@google/gemini-cli', 'gemini'),
    'opencode': ('opencode-ai', 'opencode'),
}


def clean_env(proxy=''):
    env = os.environ.copy()
    for name in PROXIES:
        env.pop(name, None)
    if proxy:
        env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, http_proxy=proxy, https_proxy=proxy)
    env.update(NO_PROXY='localhost,127.0.0.1,::1', no_proxy='localhost,127.0.0.1,::1')
    return env


def private_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


def scope(payload):
    name = payload['scope']
    if not re.fullmatch(r'[a-f0-9]{32}', name):
        raise ValueError('Invalid workspace ID')
    return Path.home() / '.local/share/edge-ssh-ai' / name


def command(args, env=None, timeout=25):
    p = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True, start_new_session=True)
    try:
        output, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
        raise RuntimeError('操作超时；已停止本次任务进程，保留安装阶段目录供排查')
    if p.returncode:
        # Tool installers never receive API keys. Bound output for diagnostics.
        raise RuntimeError(output[-3000:] or 'Command failed')
    return output.strip()


def discover_tools(root):
    """Read executable metadata only; never inspect another environment's credentials."""
    shared = root.parent
    runtime = root / 'runtime'
    if not (runtime / 'bin/node').is_file():
        runtime = next((x for x in sorted(shared.glob('*/runtime'))
                        if (x / 'bin/node').is_file()), None)
    tools = {}
    for tool, (_, executable) in TOOL_PACKAGES.items():
        candidates = [root / 'tools' / tool / 'current.json']
        candidates += sorted(shared.glob('*/tools/' + tool + '/current.json'))
        found = None
        for path in dict.fromkeys(candidates):
            try:
                value = json.loads(path.read_text())
                binary = Path(value['binary'])
                if not binary.is_absolute() or not binary.is_file() or not os.access(str(binary), os.X_OK):
                    continue
                found = dict(binary=str(binary), version=str(value.get('version', '?')),
                             source='current' if path == candidates[0] else 'other_environment')
                break
            except (OSError, ValueError, KeyError, TypeError):
                continue
        if found is None:
            binary = shutil.which(executable)
            if binary:
                found = dict(binary=binary, version='待验证', source='PATH')
        tools[tool] = found
    return dict(runtime=str(runtime) if runtime else '', tools=tools)


def reuse_tools(p):
    """Reference validated program installations, retaining all environment state locally."""
    root = scope(p)
    inventory = discover_tools(root)
    required = p.get('tools', ['codex', 'harness'])
    if any(t not in TOOL_PACKAGES for t in required):
        raise ValueError('无效的工具名称')
    if not (root / 'runtime/bin/node').is_file():
        if not inventory['runtime']:
            raise ValueError('未发现可复用的受管 Node.js，请先安装运行环境')
        runtime = Path(inventory['runtime']).resolve()
        version = command([str(runtime / 'bin/node'), '--version'])
        if not re.match(r'^v(?:2[4-9]|[3-9]\d)\.', version):
            raise ValueError('已有受管 Node.js 版本不足，请安装运行环境')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = root / 'runtime'
        if destination.exists() or destination.is_symlink():
            raise ValueError('当前环境运行目录不完整，请检查后重新安装')
        destination.symlink_to(runtime, target_is_directory=True)
    result = {}
    for tool in required:
        found = inventory['tools'][tool]
        if not found:
            raise ValueError('板卡未发现可复用的 ' + tool + '，请在 AI 工具安装')
        env = tool_env(p, root)
        env.update(CODEX_HOME=str(root / 'codex-home'), DSH_HOME=str(root / 'harness-home'),
                   DSH_TELEMETRY_MODE='DISABLED')
        version = command([found['binary'], '--version'], env=env, timeout=30)
        metadata = root / 'tools' / tool / 'current.json'
        if not metadata.is_file():
            private_write(metadata, json.dumps(dict(version=found['version'], binary=found['binary'],
                                                   reused=True)))
        result[tool] = dict(version=version, source=found['source'])
    return dict(message='已有程序已验证并接入当前环境；账号和任务未共享', tools=result)


def diagnose(p):
    root = scope(p)
    home = Path.home()
    mem = {}
    if Path('/proc/meminfo').exists():
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            if key in ('MemTotal', 'MemAvailable'):
                mem[key] = int(value.split()[0]) // 1024
    inventory = discover_tools(root)
    runtime = root / 'runtime/bin/node'
    node = str(runtime) if runtime.exists() else (
        str(Path(inventory['runtime']) / 'bin/node') if inventory['runtime'] else shutil.which('node'))
    version = command([node, '--version']) if node else '未安装'
    return dict(claude=bool(inventory['tools']['claude']), gemini=bool(inventory['tools']['gemini']),
                opencode=bool(inventory['tools']['opencode']), user=command(['id', '-un']), home=str(home), arch=platform.machine(),
                libc=' '.join(platform.libc_ver()), memory_mb=mem,
                disk_free_mb=shutil.disk_usage(str(home)).free // 1024**2,
                node=version, managed_runtime=runtime.exists(),
                codex=bool(inventory['tools']['codex']),
                tool_inventory=inventory,
                scoped_codex=(root / 'tools/codex/current.json').exists(),
                scoped_harness=(root / 'tools/harness/current.json').exists(),
                codex_configured=(root / 'launch.json').exists() and (root / 'codex-home/config.toml').exists(),
                codex_extension=any((home / '.vscode-server/extensions').glob('openai.chatgpt-*/package.json')),
                harness=bool(inventory['tools']['harness']),
                flock=bool(shutil.which('flock')),
                vscode_server=(home / '.vscode-server').exists(),
                inherited_proxy=any(os.environ.get(k) for k in PROXIES),
                shell_proxy_files=[str(x) for x in [home / '.bashrc', home / '.profile']
                                   if x.exists() and '127.0.0.1:7890' in x.read_text(errors='replace')],
                harness_service=service_module().status(root))


def service_module():
    import types
    module = types.ModuleType('edge_harness_service')
    exec(compile(SERVICE_SOURCE, 'harness-service', 'exec'), module.__dict__)
    return module


def harness_start(p):
    if not (scope(p) / 'tools/harness/current.json').is_file():
        reuse_tools(dict(p, tools=['harness']))
    return service_module().start(scope(p), p['project'], p.get('proxy', ''),
                                  p['local_port'], p.get('background', True), SERVICE_SOURCE)


def harness_status(p):
    return service_module().status(scope(p), include_token=p.get('include_token', False))


def harness_stop(p):
    return service_module().stop(scope(p))


def api_test(p):
    if p.get('reuse'):
        p = tool_credentials(p) if p.get('tool', 'codex') != 'codex' else reuse_credentials(p)
    # No environment proxies or redirects; report only bounded, redacted API errors.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    proxy = p.get('proxy', '')
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else {}), NoRedirect())
    url = p['base_url'].rstrip('/')
    headers = {'Authorization': 'Bearer ' + p['key'], 'Content-Type': 'application/json',
               'Accept': 'application/json', 'User-Agent': 'EdgeSwitch/1.1.3'}
    protocol = p['protocol']
    if protocol not in ('responses', 'chat', 'anthropic', 'gemini'):
        raise ValueError('不支持的 API 协议')
    if protocol == 'anthropic':
        headers.pop('Authorization')
        headers.update({'x-api-key': p['key'], 'anthropic-version': '2023-06-01'})
        if not url.endswith('/v1'):
            url += '/v1'
    elif protocol == 'gemini':
        headers.pop('Authorization')
        headers['x-goog-api-key'] = p['key']
    data = None
    if p.get('infer'):
        if not str(p.get('model', '')).strip():
            raise ValueError('请填写模型 ID')
        if protocol == 'responses':
            url += '/responses'
            data = dict(model=p['model'], input='Reply with OK.', max_output_tokens=64)
        elif protocol == 'anthropic':
            url += '/messages'
            data = dict(model=p['model'], messages=[dict(role='user', content='Reply with OK.')], max_tokens=64)
        elif protocol == 'gemini':
            model = urllib.parse.quote(re.sub('^models/', '', p['model']), safe='')
            url += '/models/' + model + ':generateContent'
            data = dict(contents=[dict(parts=[dict(text='Reply with OK.')])], generationConfig=dict(maxOutputTokens=64))
        else:
            url += '/chat/completions'
            data = dict(model=p['model'], messages=[dict(role='user', content='Reply with OK.')], max_tokens=64)
        data = json.dumps(data).encode()
    else:
        url += '/models'
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with opener.open(request, timeout=40) as response:
            value = json.loads(response.read(2 * 1024 * 1024))
        if not isinstance(value, dict):
            raise ValueError('响应不是 JSON 对象')
        if p.get('infer'):
            if p['protocol'] == 'responses':
                output = value.get('output', [])
                ok = value.get('status') == 'completed' and isinstance(output, list) and any(
                    isinstance(item, dict) and item.get('type') == 'message' and isinstance(item.get('content'), list) and any(
                        isinstance(part, dict) and part.get('type') == 'output_text' and bool(part.get('text'))
                        for part in item['content'])
                    for item in output)
            elif protocol == 'anthropic':
                ok = any(isinstance(item, dict) and item.get('type') == 'text' and bool(item.get('text'))
                         for item in value.get('content', []))
            elif protocol == 'gemini':
                ok = any(isinstance(item, dict) and any(isinstance(part, dict) and bool(part.get('text'))
                         for part in item.get('content', {}).get('parts', [])) for item in value.get('candidates', []))
            else:
                choices = value.get('choices', [])
                message = choices[0].get('message', {}) if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
                ok = isinstance(message, dict) and isinstance(message.get('content'), str) and bool(message['content'].strip())
            if not ok:
                return dict(ok=False, message='HTTP 成功，但未得到所选协议的完整文本回答；请检查协议或输出额度')
            return dict(ok=True, message='最小推理成功；尚未验证流式响应与工具调用')
        models = value.get('models') if protocol == 'gemini' else value.get('data')
        if not isinstance(models, list):
            return dict(ok=False, message='模型列表格式不兼容；可手填模型并测试推理')
        field = 'name' if protocol == 'gemini' else 'id'
        ids = [re.sub('^models/', '', str(x[field])) for x in models if isinstance(x, dict) and field in x]
        return dict(ok=True, message='模型列表读取成功；尚未验证推理', models=ids[:1000], truncated=len(ids) > 1000)
    except urllib.error.HTTPError as exc:
        meaning = {401: '凭据无效', 403: '访问被拒绝', 404: '地址或接口不存在',
                   429: '请求限流或额度不足'}
        content_type = exc.headers.get('Content-Type', '') if exc.headers else ''
        detail = ''
        if 'json' in content_type.lower():
            try:
                body = json.loads(exc.read(16384))
                error = body.get('error', {}) if isinstance(body, dict) else {}
                if isinstance(error, dict):
                    detail = error.get('message', '')
                elif isinstance(error, str):
                    detail = error
                if isinstance(detail, str):
                    detail = detail.replace(p['key'], '[已隐藏]')
                    detail = re.sub(r'(?i)Bearer\s+\S+|\bsk-[A-Za-z0-9_-]+', '[已隐藏]', detail)
                    detail = re.sub(r'[\x00-\x1f\x7f]', ' ', detail)[:400]
                else:
                    detail = ''
            except (ValueError, OSError):
                detail = ''
        category = 'api'
        if exc.code == 503:
            category = 'provider_unavailable'
            meaning[503] = '供应商服务暂不可用'
            if '无可用渠道' in detail:
                meaning[503] = ('供应商没有可用于此模型的渠道；先核对模型 ID 是否与模型列表完全一致。'
                                '若一致仍失败，请检查供应商分组权限、余额或渠道状态')
        if exc.code == 403 and 'json' not in content_type.lower():
            category = 'gateway'
            meaning[403] = '网关或防护层拒绝请求；不能据此判断 API Key 无效'
        return dict(ok=False, status=exc.code, category=category,
                    route='代理' if proxy else '直连',
                    message='HTTP %s：%s%s' % (exc.code, meaning.get(exc.code, '服务返回错误'),
                                              ('；服务提示：' + detail) if detail else ''))
    except (urllib.error.URLError, OSError):
        return dict(ok=False, message='网络、TLS 或代理连接失败，请先检查线路')


def install_runtime(p):
    root = scope(p)
    archive = root / 'node.tar.xz'
    if hashlib.sha256(archive.read_bytes()).hexdigest() != p['sha256']:
        raise ValueError('Node.js SHA-256 mismatch')
    stage = root / ('runtime-stage-' + str(time.time_ns()))
    stage.mkdir(parents=True)
    with tarfile.open(str(archive)) as tar:
        members = tar.getmembers()
        for item in members:
            # Official archive needs files/directories/symlinks only; reject traversal.
            target = (stage / item.name).resolve()
            if os.path.commonpath([str(stage.resolve()), str(target)]) != str(stage.resolve()):
                raise ValueError('Unsafe archive path')
            if not (item.isfile() or item.isdir() or item.issym()):
                raise ValueError('Unsupported archive entry')
            if item.issym():
                link = (target.parent / item.linkname).resolve()
                if os.path.commonpath([str(stage.resolve()), str(link)]) != str(stage.resolve()):
                    raise ValueError('Unsafe archive link')
        tar.extractall(str(stage))
    children = list(stage.iterdir())
    if len(children) != 1:
        raise ValueError('Unexpected runtime layout')
    version = command([str(children[0] / 'bin/node'), '--version'])
    dest = root / 'runtime'
    if dest.exists():
        raise RuntimeError('独立 Node 已存在；不覆盖运行中的版本')
    children[0].rename(dest)
    stage.rmdir()
    archive.unlink()
    return dict(node=version, path=str(dest))


def tool_env(p, root):
    env = clean_env(p.get('proxy', ''))
    for name in list(env):
        if re.search(r'(API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)', name, re.I):
            env.pop(name, None)
    env['PATH'] = str(root / 'runtime/bin') + os.pathsep + env.get('PATH', '')
    # Keep npm user configuration/caches scoped to this workbench.
    env['npm_config_userconfig'] = str(root / 'npmrc')
    env['npm_config_cache'] = str(root / 'npm-cache')
    env['npm_config_registry'] = 'https://registry.npmjs.org/'
    return env


def install_tool(p):
    root = scope(p)
    tool = p['tool']
    package, binary_name = TOOL_PACKAGES[tool]
    version = p['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', version):
        raise ValueError('需要明确的 npm 版本号，例如 1.2.3')
    found = discover_tools(root)['tools'][tool]
    if not (root / 'tools' / tool / 'current.json').is_file() and found and found['version'] == version:
        return reuse_tools(dict(p, tools=[tool]))
    if not (root / 'runtime/bin/node').exists():
        raise RuntimeError('请先安装独立 Node.js 运行环境')
    if shutil.disk_usage(str(Path.home())).free < 1536 * 1024**2:
        raise RuntimeError('可用空间不足 1.5 GB')
    dest = root / 'tools' / tool / version
    env = tool_env(p, root)
    if dest.exists():
        binary = dest / 'node_modules/.bin' / binary_name
        result = command([str(binary), '--version'], env, timeout=60)
        private_write(dest.parent / 'current.json', json.dumps(dict(version=version, binary=str(binary))))
        return dict(tool=tool, version=result, message='复用已安装版本')
    stage = dest.with_name(version + '.install-' + str(time.time_ns()))
    stage.mkdir(parents=True, mode=0o700)
    npm = str(root / 'runtime/bin/npm')
    command([npm, 'install', '--prefix', str(stage), '--engine-strict', '--no-audit', '--no-fund',
             '--prefer-offline', '--fetch-retries=1', '--fetch-timeout=60000',
             package + '@' + version], env, timeout=1800)
    binary = stage / 'node_modules/.bin' / binary_name
    result = command([str(binary), '--version'], env, timeout=60)
    stage.rename(dest)
    binary = dest / 'node_modules/.bin' / binary_name
    private_write(dest.parent / 'current.json', json.dumps(dict(version=version, binary=str(binary))))
    return dict(tool=tool, version=result)


def codex_history_busy(homes):
    """Migration is offline: do not rewrite files owned by an active app-server."""
    wanted = {str(home.resolve()) for home in homes}
    for process in Path('/proc').glob('[0-9]*'):
        try:
            if process.name == str(os.getpid()):
                continue
            values = (process / 'environ').read_bytes().split(b'\0')
            if any(value.startswith(b'CODEX_HOME=') and str(Path(os.fsdecode(value[11:])).resolve()) in wanted for value in values):
                return True
        except PermissionError:
            # Do not assume a matching but unreadable process is idle.
            try:
                if (process / 'status').stat().st_uid == os.getuid() and 'codex' in (process / 'comm').read_text().lower():
                    return True
            except OSError:
                pass
        except (OSError, ValueError):
            continue
    return False


def _history_fingerprint(path):
    if not path.exists():
        return None
    digest = hashlib.sha256()
    files = sorted(p for p in path.rglob('*') if p.is_file()) if path.is_dir() else [path]
    wal = path.with_name(path.name + '-wal')
    # Merely opening a WAL-mode DB can create an empty/header-only sidecar.
    # Actual frames must participate in drift detection, including uncheckpointed commits.
    if path.suffix == '.sqlite' and wal.exists() and wal.stat().st_size > 32:
        files.append(wal)
    for file in files:
        digest.update(str(file.relative_to(path) if path.is_dir() else file.name).encode())
        with file.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
    return digest.hexdigest()


def prepare_codex_history(root):
    """Share history, not credentials, inside one managed environment.

    Originals stay in a permanent backup. A staged union is published only
    after validation; reruns can finish linking after an interrupted migration.
    Both configs also set sqlite_home here, so future DB versions share storage.
    """
    import fcntl
    homes = [root / 'codex-home', root / 'codex-chatgpt-home']
    shared = root / 'codex-history'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'codex-history.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = shared / 'ready-v1.json'
        if marker.exists():
            names = json.loads(marker.read_text())['entries']
            if all((home / name).is_symlink() and (home / name).resolve() == (shared / name).resolve()
                   for home in homes for name in names):
                return
        if codex_history_busy(homes):
            raise ValueError('首次统一 Codex 历史前，请关闭此工作环境的远程 VS Code 窗口及 Codex 终端，再重新同步；原记录未改动')
        backup = root / 'codex-history-backup-v1'
        if not marker.exists():
            # Work only on copies. No auth, config or other environment is read.
            with tempfile.TemporaryDirectory(prefix='history-stage-', dir=str(root)) as directory:
                stage = Path(directory)
                names = {'sessions', 'archived_sessions', 'history.jsonl', 'session_index.jsonl'}
                for home in homes:
                    names.update(path.name for path in home.glob('state_*.sqlite'))
                fingerprints = {home.name: {name: _history_fingerprint(home / name)
                                           for name in names} for home in homes}
                for name in sorted(names):
                    sources = [home / name for home in homes if (home / name).exists()]
                    target = stage / name
                    if name.endswith('.sqlite'):
                        _merge_codex_history_db(sources, target)
                    elif name.endswith('.jsonl'):
                        seen = set()
                        with target.open('wb') as output:
                            for source in sources:
                                for line in source.read_bytes().splitlines():
                                    if line and line not in seen:
                                        output.write(line + b'\n')
                                        seen.add(line)
                    else:
                        target.mkdir(mode=0o700)
                        for source in sources:
                            for path in source.rglob('*'):
                                if not path.is_file():
                                    continue
                                dest = target / path.relative_to(source)
                                content = path.read_bytes()
                                # Only the session metadata header owns the bucket.
                                # Never rewrite message text, prompts or turn bodies.
                                if path.suffix == '.jsonl':
                                    header, sep, rest = content.partition(b'\n')
                                    try:
                                        meta = json.loads(header)
                                        payload = meta.get('payload', {})
                                        if meta.get('type') == 'session_meta' and payload.get('model_provider') in ('openai', 'edge'):
                                            payload['model_provider'] = 'edge'
                                            content = json.dumps(meta, ensure_ascii=False).encode() + sep + rest
                                    except (ValueError, AttributeError):
                                        raise ValueError('Codex 历史文件格式无效；未迁移，请保留原记录后排查') from None
                                if dest.exists() and dest.read_bytes() != content:
                                    raise ValueError('两个 Codex 历史目录存在同名不同内容的记录；未覆盖，请先处理冲突')
                                dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                                dest.write_bytes(content)
                                dest.chmod(0o600)
                    if target.is_file():
                        target.chmod(0o600)
                # Snapshot even WAL-backed databases using SQLite's backup API.
                for home in homes:
                    for name in names:
                        source = home / name
                        dest = backup / home.name / name
                        if not source.exists() or dest.exists():
                            continue
                        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        if name.endswith('.sqlite'):
                            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db, closing(sqlite3.connect(dest)) as copy:
                                db.backup(copy)
                        elif source.is_dir():
                            shutil.copytree(source, dest)
                        else:
                            shutil.copy2(source, dest)
                        if dest.is_file():
                            dest.chmod(0o600)
                # A race or interrupted previous run must never hide new data.
                if codex_history_busy(homes) or any(_history_fingerprint(home / name) != fingerprints[home.name][name]
                                                   for home in homes for name in names):
                    raise ValueError('迁移期间 Codex 历史发生变化；原记录未替换，请关闭 Codex 后重试')
                private_write(stage / 'ready-v1.json', json.dumps(dict(entries=sorted(names), sources=fingerprints)))
                stage.rename(shared)
        manifest = json.loads(marker.read_text())
        names = manifest['entries']
        # Validate every unlinked original before moving any of them on retry.
        for home in homes:
            for name in names:
                path = home / name
                if path.is_symlink() and path.resolve() == (shared / name).resolve():
                    continue
                original = backup / 'originals' / home.name / name
                source = path if path.exists() else original
                if _history_fingerprint(source) != manifest['sources'][home.name][name]:
                    raise ValueError('未完成迁移的原历史已变化；未覆盖，请保留 codex-history 与 codex-history-backup-v1 后排查')
        for home in homes:
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            for name in names:
                path = home / name
                target = shared / name
                if path.is_symlink() and path.resolve() == target.resolve():
                    continue
                if path.exists() or path.is_symlink():
                    # Keep the actual originals as well as the preflight snapshot.
                    original = backup / 'originals' / home.name / name
                    original.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if original.exists():
                        raise ValueError('历史迁移备份已存在；未覆盖，请检查未完成的迁移')
                    path.rename(original)
                # A stale sidecar must not be attached to the new shared DB.
                if name.endswith('.sqlite'):
                    for suffix in ('-wal', '-shm', '-journal'):
                        sidecar = home / (name + suffix)
                        if sidecar.exists():
                            dest = backup / 'originals' / home.name / sidecar.name
                            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                            sidecar.rename(dest)
                path.symlink_to(target, target_is_directory=target.is_dir())


def _merge_codex_history_db(sources, target):
    """Import older compatible indexes into the newest schema without downgrading."""
    def schema(db):
        return dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))

    def migrations(db, tables):
        if '_sqlx_migrations' not in tables:
            return []
        return list(db.execute('SELECT version,checksum,success FROM _sqlx_migrations ORDER BY version'))

    def quote(name):
        return '"' + name.replace('"', '""') + '"'

    # The API CLI and the VS Code bundled CLI can legitimately be different
    # releases. Pick the most advanced migration lineage before importing rows.
    inventory = []
    for source in sources:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
            tables = schema(db)
            versions = migrations(db, tables)
            if any(not row[2] for row in versions):
                raise ValueError('Codex 历史库有未成功完成的数据库升级；原记录未替换')
            inventory.append((source, tables, versions))
    inventory.sort(key=lambda item: (len(item[2]), len(item[1])), reverse=True)
    with closing(sqlite3.connect(target)) as dest:
        baseline = None
        baseline_versions = []
        for source, tables, versions in inventory:
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
                if src.execute('PRAGMA quick_check').fetchone() != ('ok',):
                    raise ValueError('Codex 历史数据库损坏；未迁移，请先保留原文件后排查')
                if not tables:
                    continue
                if baseline is None:
                    src.backup(dest)
                    baseline = tables
                    baseline_versions = versions
                    continue
                if versions or baseline_versions:
                    # Prefix versions, checksums and success flags must match.
                    # A newer schema may add nullable/default columns and tables.
                    if not versions or versions != baseline_versions[:len(versions)]:
                        raise ValueError('两个 Codex 历史库不属于兼容的数据库升级链；原记录未替换')
                elif tables != baseline:
                    raise ValueError('Codex 历史库结构不同且缺少升级记录，无法确认兼容；原记录未替换')
                # Imports happen in a private stage with foreign_keys off (SQLite
                # default); validate all relationships once every table is merged.
                for table in tables:
                    if table in ('_sqlx_migrations', 'backfill_state', 'rollout_migration_state',
                                 'rollout_migration_skipped_rollouts', 'remote_control_enrollments',
                                 'external_agent_config_imports', 'logs'):
                        # These describe this DB instance's background work or
                        # account enrollment, not conversations. Keep primary
                        # state; each complete source DB remains backed up.
                        continue
                    quoted = quote(table)
                    if table not in ('threads', 'thread_dynamic_tools', 'thread_spawn_edges', 'thread_sections',
                                     'thread_section_appearance', 'projects', 'project_roots', 'project_idempotency_keys',
                                     'thread_attachments', 'thread_artifacts', 'guardian_review_feedback',
                                     'thread_read_receipts', 'stage1_outputs', 'agent_jobs', 'agent_job_items'):
                        if src.execute('SELECT 1 FROM ' + quoted + ' LIMIT 1').fetchone():
                            raise ValueError('Codex 历史数据库包含尚未支持的数据表；未迁移，请保留原记录后更新 Edge Switch')
                        continue
                    info = list(src.execute('PRAGMA table_info(' + quoted + ')'))
                    columns = [row[1] for row in info]
                    primary = [i for i, row in enumerate(info) if row[5]]
                    target_info = list(dest.execute('PRAGMA table_info(' + quoted + ')'))
                    target_columns = {row[1]: row for row in target_info}
                    if any(name not in target_columns for name in columns):
                        raise ValueError('新版 Codex 历史库缺少旧数据字段，无法无损导入；原记录未替换')
                    if any(row[1] not in columns and row[3] and row[4] is None for row in target_info):
                        raise ValueError('新版 Codex 历史库新增必填字段且无默认值；原记录未替换')
                    if any(row[2].upper() != target_columns[row[1]][2].upper() or row[5] != target_columns[row[1]][5]
                           for row in info):
                        raise ValueError('Codex 历史字段类型或主键不兼容；原记录未替换')
                    fields = ','.join(quote(name) for name in columns)
                    for row in src.execute('SELECT ' + fields + ' FROM ' + quoted):
                        predicates = primary or list(range(len(columns)))
                        where = ' AND '.join('"' + columns[i].replace('"', '""') + '" IS ?' for i in predicates)
                        previous = dest.execute('SELECT ' + fields + ' FROM ' + quoted + ' WHERE ' + where,
                                                tuple(row[i] for i in predicates)).fetchone()
                        if previous is not None:
                            if previous != row:
                                raise ValueError('两个 Codex 历史数据库存在同编号不同内容的数据；未覆盖，请先处理冲突')
                        else:
                            try:
                                dest.execute('INSERT INTO ' + quoted + ' (' + fields + ') VALUES (' + ','.join('?' for _ in row) + ')', row)
                            except sqlite3.IntegrityError:
                                raise ValueError('Codex 历史索引存在冲突；未迁移，请保留原数据后排查') from None
        columns = {row[1] for row in dest.execute('PRAGMA table_info(threads)')}
        if 'model_provider' in columns:
            dest.execute("UPDATE threads SET model_provider='edge' WHERE model_provider='openai'")
        # Some old rollouts were never indexed. Scan the united session tree
        # again rather than retaining a completed scan of only the primary home.
        backfill = {row[1] for row in dest.execute('PRAGMA table_info(backfill_state)')}
        if {'status', 'last_watermark', 'last_success_at', 'updated_at'} <= backfill:
            dest.execute("UPDATE backfill_state SET status='pending',last_watermark=NULL,last_success_at=NULL,updated_at=?", (int(time.time()),))
        if dest.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('Codex 历史关联索引不完整；未迁移，请保留原数据后排查')
        dest.commit()
        if dest.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise ValueError('合并后的 Codex 历史索引检查失败；未替换原记录')


def configure(p):
    if p.get('reuse') and p.get('auth_mode') != 'chatgpt':
        p = reuse_credentials(p)
    root = scope(p)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not Path(p['project']).is_dir():
        raise ValueError('工程目录不存在')
    if not (root / 'tools/codex/current.json').exists():
        reuse_tools(dict(p, tools=['codex']))
    official = p.get('auth_mode') == 'chatgpt'
    if official and (root / 'launch.json').exists() and not (root / 'api-launch.json').exists():
        previous = json.loads((root / 'launch.json').read_text())
        if previous.get('key'):
            private_write(root / 'api-launch.json', json.dumps(previous))
    codex_home = root / ('codex-chatgpt-home' if official else 'codex-home')
    config = ('forced_login_method = "api"\ncli_auth_credentials_store = "file"\nmodel_provider = "edge"\nmodel = ' + json.dumps(p.get('model', '')) +
              '\n[model_providers.edge]\nname = "Edge Workbench"\nbase_url = ' +
              json.dumps(p.get('base_url', '')) + '\nwire_api = "responses"\nenv_key = "OPENAI_API_KEY"\n')
    if official:
        config = ('forced_login_method = "chatgpt"\ncli_auth_credentials_store = "file"\nmodel_provider = "edge"\n'
                  '[model_providers.edge]\nname = "OpenAI"\nrequires_openai_auth = true\nsupports_websockets = true\nwire_api = "responses"\n')
    config = 'sqlite_home = ' + json.dumps(str(root / 'codex-history')) + '\n' + config
    current = json.loads((root / 'tools/codex/current.json').read_text())
    # Parse with the installed CLI before committing a profile, without making a model request.
    with tempfile.TemporaryDirectory(prefix='config-check-', dir=str(root)) as directory:
        private_write(Path(directory) / 'config.toml', config)
        env = tool_env(p, root)
        env.update(CODEX_HOME=directory, OPENAI_API_KEY='configuration-check-only')
        command([current['binary'], 'features', 'list'], env=env, timeout=25)
    prepare_codex_history(root)
    private_write(codex_home / 'config.toml', config)
    if not official:
        private_write(codex_home / 'auth.json', json.dumps({'OPENAI_API_KEY': p['key']}))
    private_write(root / 'launch.json', json.dumps(dict(project=p['project'], key=p.get('key', '') if not official else '', auth_mode='chatgpt' if official else 'api', proxy=p.get('proxy', ''), service_id=p.get('service_id', ''))))
    if not official:
        private_write(root / 'api-launch.json', (root / 'launch.json').read_text())
    private_write(root / 'run-codex.py', CODEX_LAUNCHER)
    return dict(path=str(codex_home), message='仅配置独立 Codex 终端；未更改共享 VS Code 插件配置')


def reuse_credentials(p):
    root = scope(p)
    if p.get('reuse'):
        saved_path = root / 'api-launch.json'
        if not saved_path.exists():
            saved_path = root / 'launch.json'
        if not saved_path.exists():
            raise ValueError('此 API 尚未保存到板卡，请编辑填写 API Key 并勾选加密保存')
        saved = json.loads(saved_path.read_text())
        if p.get('service_id') and saved.get('service_id') != p['service_id']:
            if saved.get('service_id') or not p.get('legacy_service'):
                raise ValueError('此 API 尚未同步密钥；请填写该服务的 API Key，避免复用另一服务的凭据')
        config = (root / 'codex-home/config.toml').read_text()
        def setting(name):
            match = re.search(r'^' + name + r'\s*=\s*("(?:[^"\\]|\\.)*")\s*$', config, re.M)
            if not match:
                raise ValueError('已保存的 Codex 配置不完整，请在模型服务重新填写')
            return json.loads(match.group(1))
        base_url = setting('base_url')
        if p.get('base_url', '').rstrip('/') != base_url.rstrip('/'):
            raise ValueError('API 地址已改变；为避免把旧密钥发给新服务，请重新填写 API Key')
        p = dict(p, key=saved['key'], model=p.get('model') or setting('model'), base_url=base_url)
    return p


def parse_official_auth(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 256 * 1024:
        raise ValueError('auth.json 必须为不超过 256 KB 的 JSON 文本')
    try:
        auth = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('auth.json JSON 格式错误；未保存凭据') from None
    if not isinstance(auth, dict):
        raise ValueError('auth.json 必须为 JSON 对象')
    if not auth:
        return None
    tokens = auth.get('tokens')
    if not isinstance(tokens, dict) or not isinstance(tokens.get('access_token'), str) or not tokens['access_token'].strip():
        raise ValueError('请导入 ChatGPT OAuth 登录的 auth.json（需含 tokens.access_token），不能使用 API Key 配置')
    if auth.get('auth_mode') not in (None, 'chatgpt') or auth.get('OPENAI_API_KEY'):
        raise ValueError('官方账号卡片只接受 ChatGPT OAuth 凭据，请使用 API 卡片保存 API Key')
    for name in ('access_token', 'refresh_token', 'id_token', 'account_id'):
        value = tokens.get(name)
        if value is not None and (not isinstance(value, str) or any(ord(c) < 32 for c in value)):
            raise ValueError('auth.json 登录字段格式无效')
    return auth


def import_official_auth(p):
    auth = parse_official_auth(p.get('auth_json', ''))
    if auth is None:
        return dict(message='沿用板卡已有登录')
    root = scope(p)
    serialized = json.dumps(auth, sort_keys=True, separators=(',', ':'))
    fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
    marker = root / 'official-import.json'
    # Codex rotates tokens. Never overwrite refreshed remote tokens with the same old import.
    if marker.exists():
        try:
            previous = json.loads(marker.read_text())
        except ValueError:
            previous = {}
        if previous.get('fingerprint') == fingerprint and (root / 'codex-chatgpt-home/auth.json').exists():
            return dict(message='保留板卡当前登录（包括 Codex 已刷新的凭据）')
    private_write(root / 'codex-chatgpt-home/auth.json', serialized)
    private_write(marker, json.dumps(dict(fingerprint=fingerprint)))
    return dict(message='官方账号凭据已同步到此工作环境')


def official_test(p):
    root = scope(p)
    status = codex_auth_status(p)
    if not p.get('infer'):
        proxy = p.get('proxy', '')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else {}))
        request = urllib.request.Request('https://chatgpt.com/codex', headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with opener.open(request, timeout=30) as response:
                response.read(1024)
        except urllib.error.HTTPError as exc:
            body = exc.read(16384).decode('utf-8', errors='replace')
            message = ('官方返回地区限制，请检查所选网络线路' if 'unsupported_country_region_territory' in body
                       else '官方网页返回 HTTP %d；可能被网关或防护拦截，不能据此判断账号无效' % exc.code)
            return dict(ok=False, authenticated=status['authenticated'], message=message)
        except (urllib.error.URLError, OSError):
            return dict(ok=False, authenticated=status['authenticated'], message='板卡到官方网页的网络、TLS 或代理连接失败')
        return dict(ok=status['authenticated'], authenticated=status['authenticated'],
                    message='官方网页可达，CLI 已识别账号凭据；尚未验证推理' if status['authenticated']
                    else '官方网页可达，但此工作环境尚未登录；请编辑官方卡片导入或登录')
    if not status['authenticated']:
        return dict(ok=False, message='尚未登录官方账号，请先编辑官方卡片导入或登录')
    env = tool_env(p, root)
    env['CODEX_HOME'] = str(root / 'codex-chatgpt-home')
    for name in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_ACCESS_TOKEN'):
        env.pop(name, None)
    binary = json.loads((root / 'tools/codex/current.json').read_text())['binary']
    # An empty temporary project prevents reading the user's source or running project instructions.
    with tempfile.TemporaryDirectory(prefix='official-check-', dir=str(root)) as directory:
        try:
            output = command([binary, 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral',
                              '--skip-git-repo-check', '--sandbox', 'read-only', '--json', '-C', directory,
                              'Reply only OK. Do not use tools or read any files.'], env=env, timeout=75)
        except RuntimeError as exc:
            error = str(exc).lower()
            if 'unsupported_country_region_territory' in error:
                message = '官方返回地区限制，请检查所选网络线路'
            elif any(word in error for word in ('401', 'unauthorized', 'refresh_token', 'login')):
                message = '官方凭据失效或刷新失败，请重新登录或导入最新 auth.json'
            elif any(word in error for word in ('429', 'quota', 'usage limit', 'rate limit')):
                message = '官方账号额度不足或请求限流，请在 Codex 中查看账号额度'
            elif 'unexpected argument' in error:
                message = '当前 Codex 不支持验证参数，请在 AI 工具升级 Codex'
            else:
                message = '官方推理未完成，请检查网络、账号权限或服务状态；未将凭据及原始 CLI 输出写入日志'
            return dict(ok=False, message=message)
    reply = False
    complete = False
    failed = False
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get('item', {})
        if event.get('type') == 'item.completed' and isinstance(item, dict) and item.get('type') == 'agent_message':
            reply = reply or bool(item.get('text', '').strip())
        complete = complete or event.get('type') == 'turn.completed'
        failed = failed or event.get('type') in ('turn.failed', 'error')
    ok = reply and complete and not failed
    return dict(ok=ok, message='官方账号最小推理成功；尚未验证工具调用' if ok else 'CLI 未返回完整的模型回答，推理尚未通过')


def codex_auth_status(p):
    root = scope(p)
    env = tool_env(p, root)
    current = root / 'tools/codex/current.json'
    if not current.exists() or not (root / 'codex-chatgpt-home/auth.json').exists():
        return dict(authenticated=False)
    env.update(CODEX_HOME=str(root / 'codex-chatgpt-home'))
    for name in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_ACCESS_TOKEN'):
        env.pop(name, None)
    try:
        binary = json.loads(current.read_text())['binary']
        status = command([binary, 'login', 'status'], env=env, timeout=25)
    except RuntimeError:
        return dict(authenticated=False)
    return dict(authenticated=('chatgpt' in status.lower()))


def vscode_server(p):
    """Prepare a private VS Code agent folder without touching shared running servers."""
    commit = p.get('commit', '')
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Invalid VS Code commit')
    root = scope(p) / '.vscode-server'
    dest = root / 'bin' / commit
    required = ('node', 'bin/code-server', 'out/server-main.js', 'product.json')
    def complete(path):
        try:
            if not all((path / name).is_file() for name in required):
                return False
            if json.loads((path / 'product.json').read_text()).get('commit') != commit:
                return False
            command([str(path / 'node'), '--version'], env=clean_env(), timeout=10)
            return True
        except (OSError, ValueError, RuntimeError):
            return False
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Serialize this environment only. Never remove VS Code's own installation locks.
    import fcntl
    with (root / 'edge-install.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('此环境正在准备 VS Code Server，请等待本次任务完成')
        proxy = p.get('proxy', '')
        if proxy and not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', proxy):
            raise ValueError('Invalid VS Code proxy tunnel')
        setup = '#!/bin/sh\n' + '\n'.join('unset ' + name for name in PROXIES) + '\n'
        if proxy:
            setup += '\n'.join('export %s="%s"' % (name, proxy) for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')) + '\n'
        private_write(root / 'server-env-setup', setup)
        ready = complete(dest)
        reused = False
        if not ready and not dest.exists() and not dest.is_symlink():
            sources = [Path.home() / '.vscode-server/bin' / commit]
            sources += sorted(root.parent.parent.glob('*/vscode-server/bin/' + commit))
            sources += sorted(root.parent.parent.glob('*/.vscode-server/bin/' + commit))
            for source in sources:
                if source == dest or not complete(source):
                    continue
                stage = Path(tempfile.mkdtemp(prefix='reuse-', dir=str(root)))
                try:
                    # Copy immutable executable files into a private directory.
                    # Do not adopt the source's data, extensions, credentials or locks.
                    shutil.copytree(str(source), str(stage / 'server'),
                                    ignore=shutil.ignore_patterns('vscode-remote-lock.*', '*.tar.gz', '*.done'))
                    if not complete(stage / 'server'):
                        raise RuntimeError('板卡已有服务端复制后校验失败，源目录未修改')
                    (stage / 'server').rename(dest)
                    ready = reused = True
                finally:
                    shutil.rmtree(str(stage))
                break
        if p.get('install') and not ready:
            if not re.fullmatch('[a-f0-9]{32}', p['upload_id']):
                raise ValueError('Invalid upload ID')
            archive = root / (commit + '-' + p['upload_id'] + '.tar.gz')
            digest = hashlib.sha256()
            with archive.open('rb') as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
            if digest.hexdigest() != p['sha256']:
                raise RuntimeError('VS Code Server 远程 SHA-256 校验失败')
            if dest.exists() or dest.is_symlink():
                raise RuntimeError('此环境服务端目录不完整；已保留目录，请关闭该环境远程窗口后检查，未覆盖或终止进程')
            stage = Path(tempfile.mkdtemp(prefix='install-', dir=str(root)))
            try:
                with tarfile.open(str(archive)) as tar:
                    members = tar.getmembers()
                    base = stage.resolve()
                    for member in members:
                        target = (stage / member.name).resolve()
                        if (target != base and base not in target.parents) or member.isdev() or member.isfifo():
                            raise ValueError('Unsafe VS Code archive member')
                        if member.issym() or member.islnk():
                            link = (target.parent / member.linkname if member.issym() else stage / member.linkname).resolve()
                            if base not in link.parents:
                                raise ValueError('Unsafe VS Code archive link')
                    tar.extractall(str(stage))
                children = list(stage.iterdir())
                package = stage if (stage / 'node').is_file() else children[0] if len(children) == 1 else stage
                if not complete(package):
                    raise RuntimeError('VS Code Server 内容、版本或 Node 运行检查失败')
                package.rename(dest)
                ready = True
                archive.unlink()
            finally:
                if stage.exists():
                    shutil.rmtree(str(stage))
        return dict(ready=ready, reused=reused, path=str(root), arch=platform.machine(),
                    free_bytes=shutil.disk_usage(str(root)).free,
                    codex_extension=any((root / 'extensions').glob('openai.chatgpt-*/package.json')))


def ide_candidates(extension_root):
    """Only accept the actual Codex extension with a matching runnable binary."""
    arch = {'aarch64': 'linux-aarch64', 'x86_64': 'linux-x86_64'}.get(platform.machine())
    found = []
    for package in Path(extension_root).glob('openai.chatgpt-*'):
        try:
            manifest = json.loads((package / 'package.json').read_text())
            binary = package / 'bin' / arch / 'codex' if arch else None
            if (manifest.get('publisher', '').lower() != 'openai' or
                    manifest.get('name', '').lower() != 'chatgpt' or not binary or
                    not binary.is_file() or not os.access(str(binary), os.X_OK)):
                continue
            found.append((package, binary, manifest.get('version', '')))
        except (OSError, ValueError, TypeError):
            continue
    return sorted(found, key=lambda item: item[0].stat().st_mtime, reverse=True)


def prepare_ide(p):
    """Install/reuse and verify on the board, never via the Windows CLI."""
    commit = p.get('commit', '')
    if not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('Invalid VS Code commit')
    root = scope(p) / '.vscode-server'
    server = root / 'bin' / commit / 'bin/code-server'
    if not server.is_file():
        raise RuntimeError('此环境 VS Code Server 尚未准备好，无法安装远程扩展')
    extensions = root / 'extensions'
    extensions.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = clean_env(p.get('proxy', ''))
    env.update(VSCODE_AGENT_FOLDER=str(root))
    args = [str(server), '--extensions-dir', str(extensions), '--user-data-dir', str(root / 'data')]
    import fcntl
    with (root / 'edge-extension.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('此环境正在准备 Codex 扩展，请等待本次任务完成')
        reused = False
        candidates = ide_candidates(extensions)
        if not candidates:
            sources = [Path.home() / '.vscode-server/extensions']
            sources += sorted(root.parent.parent.glob('*/vscode-server/extensions'))
            sources += sorted(root.parent.parent.glob('*/.vscode-server/extensions'))
            for source in sources:
                if source == extensions:
                    continue
                for package, binary, version in ide_candidates(source):
                    try:
                        command([str(binary), '--version'], env=env, timeout=20)
                    except RuntimeError:
                        continue
                    target = extensions / package.name
                    if target.exists():
                        continue
                    stage = Path(tempfile.mkdtemp(prefix='extension-copy-', dir=str(root)))
                    try:
                        shutil.copytree(str(package), str(stage / package.name))
                        (stage / package.name).rename(target)
                    finally:
                        shutil.rmtree(str(stage))
                    reused = True
                    break
                if reused:
                    break
        # The server CLI performs extension management without starting another
        # server or connecting to a running Windows window.
        try:
            listed = command(args + ['--list-extensions'], env=env, timeout=30)
            registered = 'openai.chatgpt' in [line.strip().lower() for line in listed.splitlines()]
            if not registered or not ide_candidates(extensions):
                command(args + ['--install-extension', 'openai.chatgpt'], env=env, timeout=180)
            listed = command(args + ['--list-extensions'], env=env, timeout=30)
        except RuntimeError:
            raise RuntimeError('板卡 Codex 扩展安装或注册验证失败；请检查该环境代理和扩展市场线路。远程窗口尚未作为同步成功处理。') from None
        candidates = ide_candidates(extensions)
        if not candidates or 'openai.chatgpt' not in [line.strip().lower() for line in listed.splitlines()]:
            raise RuntimeError('板卡安装步骤已结束，但未检测到已注册且包含本机架构 CLI 的 Codex 扩展；未声明安装成功')
        package, binary, version = candidates[0]
        try:
            command([str(binary), '--version'], env=env, timeout=20)
        except RuntimeError:
            raise RuntimeError('Codex 扩展已注册，但附带 CLI 无法在此板卡运行，请检查架构和系统依赖') from None
        return dict(ready=True, reused=reused, version=version, binary=str(binary),
                    message=('已复用板卡已有 Codex 扩展' if reused else 'Codex 远程扩展已就绪') +
                            '；VS Code Server 注册与 CLI 运行检查通过')


def configure_ide(p):
    """Use the extension's own app-server binary through a scoped launcher."""
    root = scope(p)
    if p.get('auth_mode') != 'chatgpt':
        p = reuse_credentials(p)
    extension_root = root / '.vscode-server/extensions' if p.get('private_server') else Path.home() / '.vscode-server/extensions'
    candidates = [item[1] for item in ide_candidates(extension_root)]
    if not candidates:
        raise RuntimeError('请先在远程 VS Code 安装 Codex（openai.chatgpt）扩展，再同步配置')
    binary = max(candidates, key=lambda x: x.stat().st_mtime)
    configure(p)
    private_write(root / 'ide-runtime.json', json.dumps({'binary': str(binary)}))
    private_write(root / 'run-ide-codex', IDE_LAUNCHER)
    (root / 'run-ide-codex').chmod(0o700)
    env = tool_env(p, root)
    if p.get('auth_mode') == 'chatgpt':
        authenticated = codex_auth_status(p)['authenticated']
        return dict(cli=str(root / 'run-ide-codex'), auth='ChatGPT',
                    message='已确认官方 ChatGPT 登录' if authenticated else '已配置官方登录入口，尚未授权；请点击“登录 ChatGPT”完成设备授权')
    try:
        status = command([str(root / 'run-ide-codex'), 'login', 'status'], env=env, timeout=25)
    except RuntimeError:
        raise RuntimeError('扩展 CLI 未能读取 API 登录；请检查配置后重新同步') from None
    if 'api key' not in status.lower():
        raise RuntimeError('扩展 CLI 尚未确认 API 登录；未声明同步成功')
    return dict(cli=str(root / 'run-ide-codex'), auth='API Key',
                message='扩展 CLI 已确认 API 登录；请在工作台打开的新 VS Code 窗口使用')


IDE_LAUNCHER = '''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
root=Path(__file__).resolve().parent
p=json.loads((root/'launch.json').read_text())
env=os.environ.copy()
for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
    env.pop(k,None)
if p['proxy']:
    env.update(HTTP_PROXY=p['proxy'],HTTPS_PROXY=p['proxy'],http_proxy=p['proxy'],https_proxy=p['proxy'])
env.pop('OPENAI_API_KEY',None)
env.pop('OPENAI_BASE_URL',None)
env.pop('CODEX_ACCESS_TOKEN',None)
if p.get('auth_mode') != 'chatgpt':
    env['OPENAI_API_KEY']=p['key']
env.update(CODEX_HOME=str(root/('codex-chatgpt-home' if p.get('auth_mode') == 'chatgpt' else 'codex-home')),
           NO_PROXY='localhost,127.0.0.1,::1',no_proxy='localhost,127.0.0.1,::1')
env['PATH']=str(root/'runtime/bin')+os.pathsep+env.get('PATH','')
binary=json.loads((root/'ide-runtime.json').read_text())['binary']
if not Path(binary).is_file():
    raise SystemExit('Codex extension updated; sync from Edge Switch again.')
os.execve(binary,[binary]+sys.argv[1:],env)
'''


def harness_prepare(p):
    root = scope(p)
    if not (root / 'tools/harness/current.json').is_file():
        reuse_tools(dict(p, tools=['harness']))
    if not Path(p['project']).is_dir():
        raise ValueError('工程目录不存在')
    if not (root / 'tools/harness/current.json').exists():
        raise ValueError('请先安装 DeepSeek Harness')
    private_write(root / 'harness-launch.json', json.dumps(dict(project=p['project'], proxy=p.get('proxy', ''))))
    private_write(root / 'run-harness.py', HARNESS_LAUNCHER)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    return dict(port=port)


HARNESS_LAUNCHER = '''import json, os, signal, subprocess, sys, threading, time
from pathlib import Path
root = Path(__file__).resolve().parent
p = json.loads((root / 'harness-launch.json').read_text())
env = os.environ.copy()
for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
    env.pop(k, None)
if p['proxy']:
    env.update(HTTP_PROXY=p['proxy'], HTTPS_PROXY=p['proxy'], http_proxy=p['proxy'], https_proxy=p['proxy'])
env.update(DSH_HOME=str(root / 'harness-home'), DSH_TELEMETRY_MODE='DISABLED',
           NO_PROXY='localhost,127.0.0.1,::1', no_proxy='localhost,127.0.0.1,::1')
env['PATH'] = str(root / 'runtime/bin') + os.pathsep + env.get('PATH','')
os.umask(0o077)
tool = json.loads((root / 'tools/harness/current.json').read_text())
child = subprocess.Popen([tool['binary'], 'web', '--host', '127.0.0.1', '--port', sys.argv[1],
                          '--trusted-host', '127.0.0.1:' + sys.argv[2], '--no-open'],
                         cwd=p['project'], env=env, start_new_session=True)
stop = threading.Event()
def disconnected():
    sys.stdin.buffer.read()
    stop.set()
threading.Thread(target=disconnected, daemon=True).start()
for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    signal.signal(sig, lambda *args: stop.set())
while child.poll() is None and not stop.wait(0.2):
    pass
if child.poll() is None:
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
sys.exit(child.returncode or 0)
'''


CODEX_LAUNCHER = '''import json, os, sys
from pathlib import Path
root = Path(__file__).resolve().parent
p = json.loads((root / 'launch.json').read_text())
env = os.environ.copy()
for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
    env.pop(k, None)
if p['proxy']:
    env.update(HTTP_PROXY=p['proxy'], HTTPS_PROXY=p['proxy'], http_proxy=p['proxy'], https_proxy=p['proxy'])
env.pop('OPENAI_API_KEY', None)
env.pop('OPENAI_BASE_URL', None)
env.pop('CODEX_ACCESS_TOKEN', None)
if p.get('auth_mode') != 'chatgpt':
    env['OPENAI_API_KEY'] = p['key']
env.update(CODEX_HOME=str(root / ('codex-chatgpt-home' if p.get('auth_mode') == 'chatgpt' else 'codex-home')),
           NO_PROXY='localhost,127.0.0.1,::1', no_proxy='localhost,127.0.0.1,::1')
env['PATH'] = str(root / 'runtime/bin') + os.pathsep + env.get('PATH','')
tool = json.loads((root / 'tools/codex/current.json').read_text())
os.chdir(p['project'])
os.execvpe(tool['binary'], [tool['binary']] + sys.argv[1:], env)
'''


def tool_credentials(p):
    """Reuse only this tool's previously synchronized service; never another tool's key."""
    tool = p.get('tool')
    if tool not in ('claude', 'gemini', 'opencode', 'harness'):
        raise ValueError('工具无效')
    path = scope(p) / (tool + '-api.json')
    if not path.is_file():
        raise ValueError('请填写此工具服务的 API Key 后再同步')
    saved = json.loads(path.read_text())
    if not p.get('service_id') or saved.get('service_id') != p['service_id']:
        raise ValueError('此服务尚未同步密钥，不能复用其他服务的凭据')
    if not saved.get('key'):
        raise ValueError('此服务尚未保存密钥')
    return dict(p, key=saved['key'], reuse=False)


def configure_tool(p):
    """Write tool-specific private configuration; no model request or shared-home mutation."""
    tool = p.get('tool')
    if p.get('auth_mode') == 'official':
        if tool not in ('claude', 'gemini', 'opencode'):
            raise ValueError('此工具需要配置 API')
        if not Path(p['project']).is_dir():
            raise ValueError('工程目录不存在')
        root = scope(p)
        (root / (tool + '-official-home')).mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (root / 'tools' / tool / 'current.json').is_file():
            reuse_tools(dict(p, tools=[tool]))
        private_write(root / (tool + '-api.json'), json.dumps(dict(auth_mode='official', project=p['project'], proxy=p.get('proxy', ''))))
        private_write(root / ('run-' + tool + '.py'), TOOL_LAUNCHER.replace('__TOOL__', tool))
        return dict(message='已准备官方登录环境，请在终端完成账号授权', tool=tool)
    protocols = {'claude': ('anthropic',), 'gemini': ('gemini',),
                 'opencode': ('chat', 'responses', 'anthropic', 'gemini'),
                 'harness': ('chat', 'responses', 'anthropic')}
    if tool not in protocols or p.get('protocol') not in protocols[tool]:
        raise ValueError('所选工具不支持此 API 协议')
    if not str(p.get('model', '')).strip():
        raise ValueError('请填写模型 ID')
    parsed = urllib.parse.urlparse(p.get('base_url', ''))
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('API 地址需要不含账号或参数的 HTTPS URL')
    if p.get('reuse'):
        p = tool_credentials(p)
    if not p.get('key') or any(c in p['key'] for c in '\r\n'):
        raise ValueError('请填写有效 API Key')
    if not Path(p['project']).is_dir():
        raise ValueError('工程目录不存在')
    root = scope(p)
    if not (root / 'tools' / tool / 'current.json').is_file():
        reuse_tools(dict(p, tools=[tool]))
    value = {k: p[k] for k in ('service_id', 'base_url', 'model', 'protocol', 'key', 'project')}
    value['proxy'] = p.get('proxy', '')
    path = root / (tool + '-api.json')
    if tool == 'harness' and service_module().status(root).get('running'):
        if path.is_file() and json.loads(path.read_text()) == value:
            return dict(message='Harness 配置未变化，保留当前后台服务')
        raise ValueError('Harness 正在运行；请先停止，再同步新的模型服务，避免中断任务')
    if tool == 'harness':
        current = json.loads((root / 'tools/harness/current.json').read_text())
        installed = current.get('version', '')
        match = re.search(r'(\d+)\.(\d+)\.(\d+)', installed)
        if not match or tuple(map(int, match.groups())) < (0, 2, 2):
            raise ValueError('多供应商配置需要 Harness 0.2.2 或更新版本，请先查询并安装新版')
        api = {'chat': 'openai-completions', 'responses': 'openai-responses',
               'anthropic': 'anthropic-messages', 'gemini': 'google-generative-ai'}[p['protocol']]
        # JSON is valid YAML. This dedicated --patch file preserves the user's home patch/settings.
        patch = [dict(id='llm-pi-ai', config=dict(providers={
            'edge-workbench': dict(apiKeyEnv='EDGE_MODEL_API_KEY', api=api, baseURL=p['base_url'],
                                   models=[dict(id=p['model'])])})),
                 dict(id='agent-default-model', config=dict(provider='edge-workbench', model=p['model']))]
        patch_path = root / 'harness-provider.patch.yml'
        # Validate a staged patch using the actual installed CLI before committing it.
        with tempfile.TemporaryDirectory(prefix='provider-check-', dir=str(root)) as directory:
            staged = Path(directory) / 'provider.yml'
            private_write(staged, json.dumps(patch))
            env = tool_env(p, root)
            env.update(DSH_HOME=str(root / 'harness-home'), DSH_TELEMETRY_DISABLED='1')
            output = command([current['binary'], '--profile', 'web', '--patch', str(staged), '--dump-config'], env=env, timeout=60)
            if 'llm-pi-ai' not in output:
                raise ValueError('此 Harness 安装未包含多供应商适配器，请升级后重试')
        private_write(patch_path, json.dumps(patch))
    elif tool == 'opencode':
        npm = {'chat': '@ai-sdk/openai-compatible', 'responses': '@ai-sdk/openai',
               'anthropic': '@ai-sdk/anthropic', 'gemini': '@ai-sdk/google'}[p['protocol']]
        base = p['base_url'].rstrip('/')
        if p['protocol'] == 'anthropic' and not base.endswith('/v1'):
            base += '/v1'
        config = {'$schema': 'https://opencode.ai/config.json', 'model': 'edge-workbench/' + p['model'],
                  'provider': {'edge-workbench': {'npm': npm, 'name': 'Edge Workbench',
                  'options': {'baseURL': base, 'apiKey': '{env:EDGE_MODEL_API_KEY}'},
                  'models': {p['model']: {'name': p['model']}}}}}
        private_write(root / 'opencode-config.json', json.dumps(config))
    elif tool == 'gemini':
        private_write(root / 'gemini-home/.gemini/settings.json', json.dumps({
            'security': {'auth': {'selectedType': 'gemini-api-key'}}, 'model': {'name': p['model']}}))
    private_write(path, json.dumps(value))
    private_write(root / ('run-' + tool + '.py'), TOOL_LAUNCHER.replace('__TOOL__', tool))
    return dict(message=tool + ' 模型服务已同步到板卡专属目录', tool=tool, model=p['model'])


TOOL_LAUNCHER = '''import json, os, sys
from pathlib import Path
root = Path(__file__).resolve().parent
tool = '__TOOL__'
p = json.loads((root / (tool + '-api.json')).read_text())
env = os.environ.copy()
for name in list(env):
    if name.startswith(('ANTHROPIC_', 'OPENAI_', 'GOOGLE_', 'GEMINI_', 'OPENCODE_', 'CLAUDE_CODE_USE')) or any(x in name.upper() for x in ('API_KEY', 'ACCESS_TOKEN', 'AUTH_TOKEN')) or name in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
        env.pop(name, None)
if p['proxy']:
    env.update(HTTP_PROXY=p['proxy'], HTTPS_PROXY=p['proxy'], http_proxy=p['proxy'], https_proxy=p['proxy'])
env.update(NO_PROXY='localhost,127.0.0.1,::1', no_proxy='localhost,127.0.0.1,::1')
env['PATH'] = str(root / 'runtime/bin') + os.pathsep + env.get('PATH', '')
args = []
if p.get('auth_mode') == 'official':
    if tool == 'claude':
        env['CLAUDE_CONFIG_DIR'] = str(root / 'claude-official-home')
    elif tool == 'gemini':
        env['GEMINI_CLI_HOME'] = str(root / 'gemini-official-home')
    elif tool == 'opencode':
        env.update(XDG_CONFIG_HOME=str(root / 'opencode-official-home/config'), XDG_DATA_HOME=str(root / 'opencode-official-home/data'), XDG_CACHE_HOME=str(root / 'opencode-official-home/cache'))
elif tool == 'claude':
    env.update(CLAUDE_CONFIG_DIR=str(root / 'claude-home'), ANTHROPIC_API_KEY=p['key'], ANTHROPIC_BASE_URL=p['base_url'], ANTHROPIC_MODEL=p['model'])
    args = ['--model', p['model']]
elif tool == 'gemini':
    base = p['base_url'].rstrip('/')
    for suffix in ('/v1beta', '/v1'):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
    env.update(GEMINI_CLI_HOME=str(root / 'gemini-home'), GEMINI_API_KEY=p['key'], GOOGLE_GEMINI_BASE_URL=base)
    args = ['--model', p['model']]
elif tool == 'opencode':
    env.update(EDGE_MODEL_API_KEY=p['key'], OPENCODE_CONFIG=str(root / 'opencode-config.json'), OPENCODE_CONFIG_CONTENT=(root / 'opencode-config.json').read_text(), XDG_CONFIG_HOME=str(root / 'opencode-home/config'), XDG_DATA_HOME=str(root / 'opencode-home/data'), XDG_CACHE_HOME=str(root / 'opencode-home/cache'))
    args = ['--model', 'edge-workbench/' + p['model']]
else:
    raise SystemExit('Use the managed Harness web entry.')
binary = json.loads((root / 'tools' / tool / 'current.json').read_text())['binary']
os.umask(0o077)
os.chdir(p['project'])
os.execvpe(binary, [binary] + args + sys.argv[1:], env)
'''


def main():
    p = json.load(sys.stdin)
    action = p['action']
    actions = dict(diagnose=diagnose, vscode_server=vscode_server, prepare_ide=prepare_ide, reuse_tools=reuse_tools, api_test=api_test, install_runtime=install_runtime,
                   install_tool=install_tool, configure_tool=configure_tool, configure=configure, configure_ide=configure_ide, codex_auth_status=codex_auth_status, import_official_auth=import_official_auth, official_test=official_test,
                   harness_prepare=harness_prepare, harness_start=harness_start,
                   harness_status=harness_status, harness_stop=harness_stop)
    if action == 'prepare':
        root = scope(p)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = dict(path=str(root))
    else:
        result = actions[action](p)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Payload contains credentials: never print payloads/tracebacks.
        print(json.dumps(dict(error=str(exc)), ensure_ascii=False))
        sys.exit(1)
