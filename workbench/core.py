"""Local configuration, OpenSSH ownership, and board operations."""
import base64
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlsplit
import uuid
import zlib

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('LOCALAPPDATA', str(ROOT))) / 'EdgeSSHAI'
NODE_VERSION = '24.11.0'
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def vscode_data_dir(scope):
    # A distinct user-data directory forces a new editor process to read the
    # corrected server layout; --reuse-window otherwise keeps the old resolver.
    return DATA / 'vscode' / (scope + '-layout2')


def validate_url(value):
    if any(ord(c) < 32 for c in value):
        raise ValueError('API 地址不能含换行或控制字符')
    u = urlsplit(value)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError('API 地址需要 HTTPS，不能含账号、密码、查询参数或片段')
    return value.rstrip('/')


def validate_model_selection(model, catalog):
    if not model.strip():
        raise ValueError('请填写模型名称')
    if catalog and model not in catalog:
        raise ValueError('模型“%s”不在当前供应商返回的列表中。请从模型下拉框选择准确 ID，例如 %s。'
                         '若供应商已更新列表，请先重新读取。' % (model, '、'.join(catalog[:4])))


@dataclass
class Profile:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = 'LPB3568'
    host: str = '192.168.0.168'
    user: str = 'FineHigh'
    port: int = 22
    project: str = '/userdata/QMB3568'
    identity: str = ''
    base_url: str = 'https://api.deepseek.com/v1'
    model: str = ''
    protocol: str = 'chat'
    proxy: bool = False
    codex_proxy: bool | None = None
    harness_proxy: bool | None = None
    harness_background: bool = True
    proxy_port: int = 7890
    codex_version: str = ''
    harness_version: str = ''
    active_api: str = ''
    codex_auth: str = 'api'

    def __post_init__(self):
        # Preserve old profiles' single network switch on first load.
        if self.codex_proxy is None:
            self.codex_proxy = self.proxy
        if self.harness_proxy is None:
            self.harness_proxy = self.proxy

    def uses_proxy(self, tool):
        return bool(self.harness_proxy if tool == 'harness' else self.codex_proxy)

    def validate(self):
        if not re.fullmatch(r'[a-f0-9]{32}', self.id):
            raise ValueError('工作环境 ID 无效')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', self.host) or self.host.startswith('-'):
            raise ValueError('设备地址需为 IPv4 或主机名')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', self.user):
            raise ValueError('Linux 用户名无效')
        if not 1 <= self.port <= 65535 or not 1 <= self.proxy_port <= 65535:
            raise ValueError('端口范围为 1—65535')
        if not self.project.startswith('/') or any(ord(c) < 32 for c in self.project):
            raise ValueError('工程目录需要 Linux 绝对路径')
        if self.codex_auth not in ('api', 'chatgpt'):
            raise ValueError('Codex 登录方式无效')
        if self.protocol not in ('responses', 'chat'):
            raise ValueError('API 协议无效')
        validate_url(self.base_url)
        if self.identity and not Path(self.identity).is_file():
            raise ValueError('SSH 私钥文件不存在')


def protect(secret, decrypt=False):
    """DPAPI, bound to the current Windows login. Never falls back to plaintext."""
    if os.name != 'nt':
        raise RuntimeError('持久保存密钥需要 Windows DPAPI')
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]
    raw = base64.b64decode(secret, validate=True) if decrypt else secret.encode('utf-8')
    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = ctypes.string_at(target.pbData, target.cbData)
        return result.decode('utf-8') if decrypt else base64.b64encode(result).decode('ascii')
    finally:
        kernel.LocalFree(target.pbData)


class Store:
    def __init__(self, directory=DATA):
        self.directory = Path(directory)
        self.path = self.directory / 'profiles.json'
        self.client_id = uuid.uuid4().hex
        self.profiles = [Profile()]
        self.secrets = {}
        self.services = {}
        self.applied = {}
        self.official = {}
        self.selected_profile = ''
        if self.path.exists():
            value = json.loads(self.path.read_text(encoding='utf-8'))
            self.client_id = value['client_id']
            self.profiles = [Profile(**p) for p in value['profiles']]
            self.secrets = value.get('secrets', {})
            self.services = value.get('services', {})
            self.applied = value.get('applied', {})
            self.official = value.get('official', {})
            self.selected_profile = value.get('selected_profile', '')
        if not re.fullmatch(r'[a-f0-9]{32}', self.client_id) or not self.profiles:
            raise ValueError('本地配置损坏')
        for p in self.profiles:
            if not p.active_api and self.services.get(p.id) != []:
                p.active_api = p.id
            if p.id not in self.services:
                self.services[p.id] = [dict(id=p.active_api, name='默认 API', base_url=p.base_url,
                                           model=p.model, protocol=p.protocol)]

    def api_entries(self, profile):
        if profile.id not in self.services:
            profile.active_api = profile.id
            self.services[profile.id] = [dict(id=profile.id, name='默认 API', base_url=profile.base_url,
                                            model=profile.model, protocol=profile.protocol)]
        return self.services[profile.id]

    def secret_id(self, profile):
        return profile.active_api or profile.id

    def scope(self, profile):
        return hashlib.sha256((self.client_id + profile.id).encode()).hexdigest()[:32]

    def save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        value = dict(client_id=self.client_id, profiles=[asdict(p) for p in self.profiles], secrets=self.secrets, services=self.services, applied=self.applied, official=self.official, selected_profile=self.selected_profile)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        for attempt in range(8):
            try:
                os.replace(tmp, self.path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                # Windows virus scanners can briefly lock a freshly replaced file.
                time.sleep(.05 * (attempt + 1))

    def key(self, profile):
        value = self.secrets.get(self.secret_id(profile))
        return protect(value, decrypt=True) if value else ''


class Board:
    def __init__(self, profile, scope):
        profile.validate()
        self.profile, self.scope = profile, scope

    def options(self, batch=True):
        p = self.profile
        args = ['-o', 'StrictHostKeyChecking=' + ('yes' if batch else 'ask'), '-o', 'ConnectTimeout=10',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']
        if batch:
            args += ['-o', 'BatchMode=yes']
        if p.identity:
            args += ['-i', p.identity, '-o', 'IdentitiesOnly=yes']
        return args

    def ssh(self, batch=True):
        binary = shutil.which('ssh.exe') or shutil.which('ssh')
        if not binary:
            raise RuntimeError('未找到系统 OpenSSH 客户端')
        return [binary, *self.options(batch), '-p', str(self.profile.port),
                '-l', self.profile.user, self.profile.host]

    def call(self, action, timeout=60, **payload):
        source = (ROOT / 'workbench/remote_agent.py').read_bytes()
        service = (ROOT / 'workbench/harness_service.py').read_bytes()
        source = b'SERVICE_SOURCE = ' + repr(service).encode('ascii') + b'\n' + source
        # Source is fixed; API credentials travel through encrypted SSH stdin only.
        # Keep the expanding remote agent off Windows' bounded command line.
        code = 'import base64,zlib,sys;exec(compile(zlib.decompress(base64.b64decode(sys.stdin.buffer.readline())),"edge-agent","exec"))'
        command = 'python3 -c ' + shlex.quote(code)
        data = base64.b64encode(zlib.compress(source)) + b'\n' + json.dumps(dict(payload, action=action, scope=self.scope)).encode()
        p = subprocess.run([*self.ssh(), command], input=data, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        try:
            result = json.loads(p.stdout.decode('utf-8'))
        except (ValueError, UnicodeError):
            # SSH diagnostics contain no API payload; never echo stdout from login scripts.
            error = p.stderr.decode('utf-8', errors='replace')[-1500:]
            if 'Permission denied' in error:
                raise RuntimeError('SSH 身份验证失败（与 API Key 无关）。请在工作环境点击“配置 SSH 登录”，使用这台电脑的密钥完成首次授权；或编辑环境选择已授权的 SSH 私钥。\n' + error) from None
            if 'Host key verification failed' in error:
                raise RuntimeError('SSH 主机指纹尚未信任或已变化。请使用“配置 SSH 登录”核验指纹；若指纹变化，请先确认板卡身份。') from None
            raise RuntimeError(error or 'SSH 未返回有效结果；请检查 Python3 和 shell 启动输出') from None
        if p.returncode or 'error' in result:
            raise RuntimeError(result.get('error', '远程操作失败'))
        return result

    def console(self, command=None):
        args = self.ssh(batch=False)
        args.insert(1, '-tt')
        if command:
            args.append(command)
        return subprocess.Popen(args, creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))

    def enroll_ssh(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        key = directory / ('id_ed25519_' + self.scope)
        if not key.exists():
            binary = shutil.which('ssh-keygen.exe') or shutil.which('ssh-keygen')
            if not binary:
                raise RuntimeError('未找到 ssh-keygen，请安装 Windows OpenSSH 客户端')
            result = subprocess.run([binary, '-t', 'ed25519', '-N', '', '-C', 'EdgeAI-' + self.scope,
                                     '-f', str(key)], capture_output=True, creationflags=CREATE_NO_WINDOW)
            if result.returncode:
                raise RuntimeError('创建本机 SSH 密钥失败：' + result.stderr.decode(errors='replace')[-500:])
        public = key.with_suffix('.pub').read_text(encoding='utf-8').strip()
        if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?', public):
            raise RuntimeError('本机 SSH 公钥格式无效')
        from dataclasses import replace
        target = Board(replace(self.profile, identity=str(key)), self.scope)
        # No stdin pipe: ssh reads host fingerprint confirmation/password directly from the terminal.
        script = ('umask 077; mkdir -p ~/.ssh && chmod 700 ~/.ssh && '
                  'touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && '
                  '(grep -qxF -- ' + shlex.quote(public) + ' ~/.ssh/authorized_keys || '
                  "printf '\\n%s\\n' " + shlex.quote(public) + ' >> ~/.ssh/authorized_keys)')
        process = target.console(script)
        try:
            status = process.wait(timeout=300)
        except subprocess.TimeoutExpired:
            process.terminate()
            raise RuntimeError('首次 SSH 授权等待超时，请重新点击配置 SSH 登录') from None
        if status:
            raise RuntimeError('首次 SSH 授权未完成，请核对用户名、板卡密码及主机指纹后重试')
        target.call('diagnose')
        return str(key)

    def install_runtime(self, log):
        info = self.call('diagnose')
        if info['arch'] not in ('aarch64', 'x86_64'):
            raise RuntimeError('目前仅支持 ARM64 / x86_64 Linux')
        if info['managed_runtime']:
            return dict(message='独立 Node 已安装', node=info['node'])
        if info.get('tool_inventory', {}).get('runtime'):
            return self.call('reuse_tools', tools=[])
        if info['disk_free_mb'] < 1536:
            raise RuntimeError('板卡可用空间不足 1.5 GB')
        libc = re.search(r'glibc\s+(\d+)\.(\d+)', info['libc'])
        if not libc or tuple(map(int, libc.groups())) < (2, 28):
            raise RuntimeError('独立 Node 需要 glibc ≥ 2.28')
        arch = 'arm64' if info['arch'] == 'aarch64' else 'x64'
        filename = 'node-v%s-linux-%s.tar.xz' % (NODE_VERSION, arch)
        base = 'https://nodejs.org/dist/v' + NODE_VERSION + '/'
        cache = DATA / 'downloads'
        cache.mkdir(parents=True, exist_ok=True)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({
            'https': 'http://127.0.0.1:' + str(self.profile.proxy_port)
        } if self.profile.proxy else {}))
        log('读取 Node.js 官方校验信息…')
        with opener.open(base + 'SHASUMS256.txt', timeout=30) as response:
            lines = response.read().decode().splitlines()
        checksums = {line.split()[1]: line.split()[0] for line in lines if len(line.split()) == 2}
        digest = checksums[filename]
        archive = cache / filename
        if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
            log('下载独立 Node.js；不替换板卡系统 Node…')
            temp = archive.with_suffix('.download')
            with opener.open(base + filename, timeout=45) as response, temp.open('wb') as target:
                shutil.copyfileobj(response, target)
            if hashlib.sha256(temp.read_bytes()).hexdigest() != digest:
                raise RuntimeError('Node.js SHA-256 校验失败')
            os.replace(temp, archive)
        remote = self.call('prepare')['path'] + '/node.tar.xz'
        log('上传并在板卡再次校验…')
        scp = shutil.which('scp.exe') or shutil.which('scp')
        if not scp:
            raise RuntimeError('未找到 scp')
        p = subprocess.run([scp, *self.options(), '-P', str(self.profile.port), str(archive),
                            self.profile.user + '@' + self.profile.host + ':' + remote],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
                           creationflags=CREATE_NO_WINDOW)
        if p.returncode:
            raise RuntimeError(p.stderr.decode(errors='replace')[-1500:])
        return self.call('install_runtime', timeout=180, sha256=digest)


class Tunnel:
    """Own one Popen handle, with SSH atomically allocating its remote port."""
    def __init__(self, board, local_port, remote_port=0):
        with socket.create_connection(('127.0.0.1', local_port), timeout=3):
            pass
        args = board.ssh()
        args[1:1] = ['-N', '-o', 'ExitOnForwardFailure=yes', '-o', 'LogLevel=DEBUG1', '-R',
                     '127.0.0.1:%d:127.0.0.1:%d' % (remote_port, local_port)]
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        self.port = None
        self.requested_port = remote_port
        self.error = ''
        self._ready = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()
        if not self._ready.wait(15) or not self.port or self.process.poll() is not None:
            self.close()
            raise RuntimeError(self.error or '代理隧道建立超时')

    def _read(self):
        for raw in self.process.stderr:
            line = raw.decode(errors='replace')
            match = re.search(r'Allocated port (\d+)', line)
            if match:
                self.port = int(match.group(1))
                self._ready.set()
            elif self.requested_port and 'remote forward success' in line:
                self.port = self.requested_port
                self._ready.set()
            else:
                if not line.startswith('debug'):
                    self.error = line.strip()
        self._ready.set()

    @property
    def alive(self):
        return self.process.poll() is None

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)


class ManagedTunnel:
    """Reconnect the same remote port, so running tools need no environment rewrite."""
    def __init__(self, board, local_port, remote_port=0, log=None, factory=Tunnel):
        self.board, self.local_port, self.factory = board, local_port, factory
        self.log = log or (lambda text: None)
        self.stop_event = threading.Event()
        self.connection = factory(board, local_port, remote_port=remote_port)
        self.port = self.connection.port
        self.state = '已连接'
        self.signature = (board.profile.host, board.profile.port, board.profile.user,
                          board.profile.identity, local_port)
        self.thread = threading.Thread(target=self._monitor, daemon=True)
        self.thread.start()

    @property
    def alive(self):
        return not self.stop_event.is_set() and self.connection.alive and self.state == '已连接'

    def _monitor(self):
        delay = 1
        while not self.stop_event.wait(1):
            if self.connection.alive:
                try:
                    with socket.create_connection(('127.0.0.1', self.local_port), timeout=2):
                        pass
                    if self.state != '已连接':
                        self.log('本机代理已恢复，板卡端口保持不变：%d' % self.port)
                    self.state, delay = '已连接', 1
                except OSError:
                    self.state = '本机代理不可用，等待恢复'
                continue
            self.state = '断线，正在自动重连'
            if self.stop_event.wait(delay):
                break
            try:
                entry = self.factory(self.board, self.local_port, remote_port=self.port)
                if self.stop_event.is_set():
                    entry.close()
                    break
                self.connection = entry
                self.state, delay = '已连接', 1
                self.log('代理隧道已自动恢复，继续使用板卡端口 %d' % self.port)
            except Exception:
                delay = min(delay * 2, 30)
                self.state = '重连等待（%d 秒）' % delay

    def close(self):
        self.stop_event.set()
        self.connection.close()
        self.thread.join(timeout=20)
        if self.thread.is_alive():
            raise RuntimeError('隧道仍在结束连接，请稍后重试')
        self.state = '已停止'


def connect_proxy(board, log=None):
    """Persist only the owned port and target identity, never a credential."""
    cache = DATA / 'network' / (board.scope + '.json')
    signature = [board.profile.host, board.profile.port, board.profile.user,
                 board.profile.identity, board.profile.proxy_port]
    port = 0
    try:
        saved = json.loads(cache.read_text())
        if saved['target'] == signature:
            port = int(saved['port'])
    except (OSError, ValueError, KeyError):
        pass
    if not 0 <= port <= 65535:
        port = 0
    entry = ManagedTunnel(board, board.profile.proxy_port, remote_port=port, log=log)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        temp = cache.with_suffix('.tmp')
        temp.write_text(json.dumps(dict(target=signature, port=entry.port)))
        os.replace(temp, cache)
    except Exception:
        entry.close()
        raise
    return entry


class LocalForward:
    def __init__(self, board, local_port, remote_port):
        args = board.ssh()
        args[1:1] = ['-N', '-o', 'ExitOnForwardFailure=yes', '-o', 'LogLevel=DEBUG1', '-L',
                     '127.0.0.1:%d:127.0.0.1:%d' % (local_port, remote_port)]
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        ready = threading.Event()
        self.error = ''
        def read():
            for raw in self.process.stderr:
                line = raw.decode(errors='replace')
                if 'Local forwarding listening on 127.0.0.1 port' in line:
                    ready.set()
                elif not line.startswith('debug') and 'channel ' not in line:
                    self.error = line.strip()
            ready.set()
        threading.Thread(target=read, daemon=True).start()
        if not ready.wait(15) or not self.alive:
            self.close()
            raise RuntimeError(self.error or '网页转发建立失败；请检查本机端口是否被占用')

    @property
    def alive(self):
        return self.process.poll() is None

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)


class PersistentHarness:
    """A reconnectable local view of a separately owned board process."""
    def __init__(self, board, proxy='', log=None, existing=None):
        self.board, self.proxy = board, proxy
        self.background = board.profile.harness_background
        self.log = log or (lambda text: None)
        self.stop_event = threading.Event()
        self.connection = None
        self.thread = None
        self.state = '启动中'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        info = existing or board.call('harness_start', project=board.profile.project, proxy=proxy,
                                      local_port=port, background=self.background)
        if existing:
            self.proxy = info.get('proxy', '')
            self.background = info.get('background', True)
        self.port, self.remote_port = info['local_port'], info['port']
        self.remote_pid = info['pid']
        self.token = info.get('token', '')
        try:
            self.connection = LocalForward(board, self.port, self.remote_port)
            self._wait_ready()
        except Exception:
            self.detach()
            # Keep the supervised process for diagnosis or reattachment. Do not erase jobs.
            raise
        self.state = '已连接'
        self.thread = threading.Thread(target=self._monitor, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return 'http://127.0.0.1:%d/?token=%s' % (self.port, self.token)

    @property
    def alive(self):
        return not self.stop_event.is_set() and self.connection is not None and self.connection.alive and self.state == '已连接'

    def _wait_ready(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor())
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            info = self.board.call('harness_status', include_token=True)
            if not info['running']:
                raise RuntimeError('Harness 已退出，请检查专属目录中的 harness-service.log')
            self.token = info.get('token', '')
            if self.token:
                try:
                    with opener.open(self.url, timeout=3) as response:
                        if response.status == 200:
                            return
                except OSError:
                    pass
            if self.stop_event.wait(2):
                raise RuntimeError('网页连接已取消')
        raise RuntimeError('Harness 网页尚未就绪；后台进程已保留，可刷新概览或重新打开')

    def _monitor(self):
        delay = 1
        checked = time.monotonic()
        while not self.stop_event.wait(2):
            if self.connection and self.connection.alive:
                if time.monotonic() - checked >= 15:
                    try:
                        info = self.board.call('harness_status', timeout=12)
                        if not info['running'] or info['pid'] != self.remote_pid:
                            self.state = '板卡服务已停止，请重新打开'
                            self.connection.close()
                            return
                        self.state = '已连接'
                    except Exception:
                        self.state = '暂时无法确认板卡服务，等待恢复'
                    checked = time.monotonic()
                continue
            self.state = '网页断线，自动重连中'
            if self.stop_event.wait(delay):
                break
            try:
                info = self.board.call('harness_status', timeout=12, include_token=True)
                if not info['running'] or info['pid'] != self.remote_pid:
                    self.state = '板卡服务已停止，请重新打开'
                    return
                entry = LocalForward(self.board, self.port, self.remote_port)
                if self.stop_event.is_set():
                    entry.close()
                    return
                self.connection = entry
                self.state, delay = '已连接', 1
                self.log('Harness 网页转发已自动恢复，可刷新原浏览器页面')
            except Exception:
                delay = min(delay * 2, 30)

    def detach(self):
        self.stop_event.set()
        if self.connection:
            self.connection.close()
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                raise RuntimeError('网页连接仍在结束，请稍后重试')

    def close(self):
        self.detach()
        if not self.background:
            self.board.call('harness_stop', timeout=25)


def open_vscode(board, data_dir, api=None, proxy='', progress=None):
    # Separate local VS Code settings: no changes to user's global SSH config.
    from workbench.editor_runtime import editor_for
    p = board.profile
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    config = data_dir / 'ssh_config'
    alias = 'edge-' + board.scope
    progress = progress or (lambda message: None)
    def quoted(value):
        value = str(value)
        if any(c in value for c in '\r\n"'):
            raise ValueError('SSH 配置路径含无效字符')
        return '"' + value.replace('\\', '/') + '"'
    text = ('Host %s\n  HostName %s\n  User %s\n  Port %d\n  StrictHostKeyChecking yes\n'
            '  ConnectTimeout 15\n  ServerAliveInterval 15\n  ServerAliveCountMax 3\n'
            '  BatchMode yes\n  RequestTTY no\n' %
            (alias, p.host, p.user, p.port))
    if p.identity:
        text += '  IdentityFile %s\n  IdentitiesOnly yes\n' % quoted(p.identity)
    config.write_text(text, encoding='utf-8')
    settings = data_dir / 'User/settings.json'
    settings.parent.mkdir(parents=True, exist_ok=True)
    from jsonc_utils import patch_jsonc
    value = settings.read_text(encoding='utf-8-sig') if settings.exists() else '{}'
    progress('VS Code：正在检查 SSH 与板卡运行条件…')
    info = board.call('diagnose')
    if p.codex_auth == 'chatgpt':
        api = dict(auth_mode='chatgpt', proxy=proxy)
    if api is None and info.get('codex_configured'):
        api = dict(reuse=True, base_url=p.base_url, model=p.model, proxy=proxy,
                   service_id=p.active_api, legacy_service=(p.active_api == p.id or not p.active_api))
    changes = {
        'remote.SSH.configFile': str(config), 'remote.SSH.localServerDownload': 'always',
        'remote.SSH.path': board.ssh()[0], 'remote.SSH.useFlock': info['flock'],
        'remote.SSH.remotePlatform': {alias: 'linux'},
        'remote.SSH.useLocalServer': False, 'remote.SSH.useExecServer': False,
        'remote.SSH.showLoginTerminal': True, 'remote.SSH.connectTimeout': 30
    }
    settings.write_text(patch_jsonc(value, changes), encoding='utf-8')
    progress('VS Code：正在使用专用 SSH 配置验证登录…')
    probe = subprocess.run([board.ssh()[0], '-F', str(config), '-T', alias, 'true'],
                           capture_output=True, timeout=25, creationflags=CREATE_NO_WINDOW)
    if probe.returncode:
        raise RuntimeError('VS Code 专用 SSH 配置登录失败，请重新配置 SSH 登录或检查密钥、主机指纹和网络；'
                           '工作台没有把打开窗口当作连接成功。')
    exe, cli, env, commit = editor_for(DATA / 'editor', progress)
    # Settings belong to Edge Switch's user-data directory only.
    changes.update({'update.mode': 'none', 'extensions.autoUpdate': False,
                    'extensions.autoCheckUpdates': False})
    args = [str(exe), str(cli), '--user-data-dir', str(data_dir),
            '--extensions-dir', str(DATA / 'editor' / commit / 'managed-extensions')]
    from workbench.vscode_server import prepare_server
    progress('VS Code：正在准备此环境独立的服务端目录…')
    server = prepare_server(board, commit, DATA / 'downloads/vscode', progress, proxy=proxy)
    local_proxy = 'http://127.0.0.1:' + str(p.proxy_port) if p.proxy else ''
    # Remote SSH appends '.vscode-server' to this base directory.
    changes.update({'remote.SSH.serverInstallPath': {alias: str(Path(server['path']).parent).replace('\\', '/')},
                    'remote.SSH.localServerDownload': 'auto',
                    'http.proxy': local_proxy,
                    'remote.SSH.httpProxy': {alias: proxy},
                    'remote.SSH.httpsProxy': {alias: proxy}})
    value = settings.read_text(encoding='utf-8-sig')
    settings.write_text(patch_jsonc(value, changes), encoding='utf-8')
    progress('VS Code：正在检查本机 Remote SSH 扩展（首次安装需访问扩展市场）…')
    # Reusing an installed extension avoids a needless Marketplace network request.
    installed = subprocess.run([*args, '--list-extensions'], env=env, capture_output=True,
                               timeout=30, creationflags=CREATE_NO_WINDOW)
    extensions = installed.stdout.decode(errors='replace').lower().splitlines()
    if installed.returncode or 'ms-vscode-remote.remote-ssh' not in [x.strip() for x in extensions]:
        result = subprocess.run([*args, '--install-extension', 'ms-vscode-remote.remote-ssh'], env=env,
                                capture_output=True, timeout=180, creationflags=CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError('Remote SSH 扩展下载/安装失败，请检查电脑到 VS Code 扩展市场的网络；板卡 SSH 与模型 API 是独立线路')
    if api:
        progress('VS Code：正在准备远程 Codex 扩展与当前环境认证…')
        if p.codex_auth != 'chatgpt' and p.protocol != 'responses':
            raise ValueError('Codex 插件需要 Responses 协议')
        extension = board.call('prepare_ide', commit=commit, proxy=proxy, timeout=300)
        if not extension.get('ready'):
            raise RuntimeError('板卡 Codex 扩展尚未验证就绪，未同步登录配置')
        progress('VS Code：' + extension['message'])
        ide = board.call('configure_ide', project=p.project, private_server=True, **api)
        changes.update({'chatgpt.cliExecutable': ide['cli'], 'chatgpt.openOnStartup': True,
                        'chatgpt.localeOverride': 'zh-CN'})
        value = settings.read_text(encoding='utf-8-sig')
        settings.write_text(patch_jsonc(value, changes), encoding='utf-8')
    progress('VS Code：远程准备已完成，正在打开远程窗口…')
    subprocess.run([*args, '--reuse-window', '--remote', 'ssh-remote+' + alias, p.project],
                   env=env, check=True, timeout=30, creationflags=CREATE_NO_WINDOW)
    progress('VS Code：打开请求已发送，实际连接状态可点击“连接诊断”查看。')
    return ('已发送 VS Code Remote SSH 打开/切换请求；' +
            ((ide.get('message', 'Codex 扩展 CLI 已确认 API 登录') + '；') if api else '') +
            '连接状态请查看 VS Code。目标工程：' + p.project)


def vscode_connection_report(data_dir):
    """Summarize connection stages without exposing raw logs, tokens, or server output."""
    data_dir = Path(data_dir)
    logs = list((data_dir / 'logs').glob('*/window*/exthost/output_logging_*/*Remote*SSH*.log'))
    if not logs:
        return dict(message='尚未找到此环境的 Remote - SSH 日志。若已打开 VS Code，请点击左下角远程连接状态；'
                    '若出现工作区信任或登录终端，请先完成提示。首次安装还需电脑可访问 VS Code 扩展市场。')
    latest = max(logs, key=lambda path: path.stat().st_mtime)
    with latest.open('rb') as source:
        source.seek(max(0, latest.stat().st_size - 128 * 1024))
        content = source.read().decode('utf-8', errors='replace').lower()
    attempt = content.rfind('ssh resolver called for')
    if attempt >= 0:
        content = content[attempt:]
    connections = list(re.finditer(r'resolved .* to port|tunneled port .* to local port', content))
    connected_at = connections[-1].start() if connections else -1
    rules = [
        (('permission denied',), 'SSH 身份验证失败；请重新配置此电脑的 SSH 登录。'),
        (('host key verification failed', 'remote host identification has changed'), 'SSH 主机指纹不匹配或未确认，请核验板卡身份。'),
        (('administratively prohibited', 'tcp forwarding is disabled'), 'SSH 登录成功但端口转发被服务端禁止，请检查 sshd 转发权限。'),
        (('could not resolve hostname',), 'SSH 主机名无法解析，请检查当前环境地址与专用 SSH 配置。'),
        (('connection timed out', 'banner exchange'), 'SSH 网络连接或握手超时，请检查局域网、SSH 服务及板卡负载。'),
        (('failed to download', 'download failed', 'localdownloadfailed', 'failed to fetch', 'getaddrinfo', 'xhr failed'), '电脑下载 VS Code Server 或扩展失败；SSH 登录与 API Key 无需重配。请启用电脑代理、确认 FlClash 正在运行，再用新版工作台重新打开。'),
        (('downloading vs code server locally', 'waiting for client to transfer server archive'), 'SSH 已登录；正在等待电脑下载 / 上传 VS Code Server，尚未建立编辑器连接。请检查电脑下载线路与代理；新版工作台会显示下载进度并复用已校验缓存。'),
        (('failed to parse remote port',), '已进入 SSH 启动流程，但 VS Code Server 未返回有效端口；请检查登录终端和服务端启动条件。'),
        (('waiting for server log', 'waiting for server to start'), 'SSH 已进入服务端启动阶段，正在等待 VS Code Server；检查板卡负载、磁盘和启动日志。'),
    ]
    for markers, message in rules:
        if any(content.rfind(marker) > connected_at for marker in markers):
            return dict(message=message, log_file=str(latest), updated=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(latest.stat().st_mtime)))
    connected = connected_at >= 0
    return dict(message='此日志包含远程连接建立记录；若页面仍在加载，请检查远程扩展或工作区负载。' if connected else
                '已生成 Remote - SSH 日志，但未检测到连接完成。请在 VS Code 输出面板选择 Remote - SSH 查看本次末尾错误。',
                log_file=str(latest), updated=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(latest.stat().st_mtime)))


class HarnessSession:
    def __init__(self, board, proxy=''):
        remote_port = board.call('harness_prepare', project=board.profile.project, proxy=proxy)['port']
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.url = 'http://127.0.0.1:' + str(self.port)
        self.token = ''
        script = '~/.local/share/edge-ssh-ai/' + board.scope + '/run-harness.py'
        args = board.ssh()
        args[1:1] = ['-T', '-o', 'ExitOnForwardFailure=yes', '-L',
                     '127.0.0.1:%d:127.0.0.1:%d' % (self.port, remote_port)]
        args.append('python3 -u ' + script + ' ' + str(remote_port) + ' ' + str(self.port))
        self.lines = []
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
        threading.Thread(target=self._read, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor())
        end = time.monotonic() + 150
        while time.monotonic() < end:
            if not self.alive:
                self.close()
                raise RuntimeError('Harness 启动失败：' + '\n'.join(self.lines[-8:]))
            try:
                target = self.url + ('/?token=' + self.token if self.token else '/')
                with opener.open(target, timeout=2) as response:
                    if response.status == 200:
                        self.url = target
                        return
            except OSError:
                pass
            time.sleep(.5)
        self.close()
        raise RuntimeError('Harness 未在 150 秒内返回可用页面：' + '\n'.join(self.lines[-8:]))

    def _read(self):
        for line in self.process.stdout:
            text = line.decode(errors='replace').strip()
            token = re.search(r'[?&]token=([A-Za-z0-9_-]+)', text)
            if token:
                self.token = token.group(1)
                text = text.replace(token.group(1), '[已隐藏]')
            if 'channel ' in text and 'Connection refused' in text:
                continue
            self.lines.append(text)
            self.lines[:] = self.lines[-30:]

    @property
    def alive(self):
        return self.process.poll() is None

    def close(self):
        # EOF tells our remote supervisor to stop only its own child process group.
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
