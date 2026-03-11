"""
OpenAI 账号协议注册（纯 HTTP API 版）
直接调用 OpenAI 认证接口完成注册流程，无需浏览器。
通过 Outlook IMAP （支持 OAuth2 XOAUTH2）获取验证码。

用法:
    python api_register.py                         # 默认配置
    python api_register.py --proxy http://ip:port  # 指定代理
    python api_register.py --workers 5             # 并发数
"""

import base64
import hashlib
import imaplib
import json
import logging
import os
import random
import requests
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Optional, Callable
from urllib.error import HTTPError, URLError
import email as email_module
import html as html_module
import argparse
from curl_cffi import requests as cffi_requests

# ═══════════════════════════════════════════════════════
# 常量配置
# ═══════════════════════════════════════════════════════
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# OpenAI OAuth
OAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAI_AUTH_URL = "https://auth.openai.com/oauth/authorize"
OAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAI_SENTINEL_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
OAI_SIGNUP_URL = "https://auth.openai.com/api/accounts/authorize/continue"
OAI_SEND_OTP_URL = "https://auth.openai.com/api/accounts/passwordless/send-otp"
OAI_VERIFY_OTP_URL = "https://auth.openai.com/api/accounts/email-otp/validate"
OAI_CREATE_URL = "https://auth.openai.com/api/accounts/create_account"
OAI_WORKSPACE_URL = "https://auth.openai.com/api/accounts/workspace/select"

LOCAL_CALLBACK_PORT = 1455
LOCAL_REDIRECT_URI = f"http://localhost:{LOCAL_CALLBACK_PORT}/auth/callback"

# 文件路径
DEFAULT_ACCOUNTS_FILE = os.path.join(SCRIPT_DIR, "accounts.txt")
RESULTS_DIR = os.path.join(SCRIPT_DIR, "tokens")
LOG_FILE = os.path.join(SCRIPT_DIR, "api_register.log")

# 超时与重试
IMAP_POLL_TIMEOUT = 180
OTP_RESEND_INTERVAL = 25
MAX_RETRY_PER_ACCOUNT = 2



# ═══════════════════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════════════════
def _setup_logger():
    logger = logging.getLogger("api_reg")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger

log = _setup_logger()


# ═══════════════════════════════════════════════════════
# 账号数据结构
# ═══════════════════════════════════════════════════════
@dataclass
class MailAccount:
    """Outlook 邮箱账号（支持密码和 OAuth2 两种认证）"""
    email: str
    password: str
    client_id: str = ""
    refresh_token: str = ""
    is_freemail: bool = False

    @classmethod
    def parse(cls, line: str) -> "MailAccount":
        """解析 accounts.txt 的一行: email----password[----client_id----refresh_token]"""
        fields = [f.strip() for f in line.strip().split("----")]
        if len(fields) < 2:
            raise ValueError(f"格式不对，至少需要 邮箱----密码，收到: {line[:60]}")
        return cls(
            email=fields[0],
            password=fields[1],
            client_id=fields[2] if len(fields) > 2 and fields[2] else "",
            refresh_token=fields[3] if len(fields) > 3 and fields[3] else "",
        )


def load_accounts_file(path: str) -> list[MailAccount]:
    """加载账号文件"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"账号文件不存在: {path}")
    result = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                result.append(MailAccount.parse(line))
            except ValueError as e:
                log.warning(f"跳过: {e}")
    return result


def get_finished_emails(directory: str) -> set[str]:
    """扫描结果目录，获取已完成的邮箱集合（用于断点续跑）"""
    done = set()
    if not os.path.isdir(directory):
        return done
    for f in os.listdir(directory):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, f), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            addr = data.get("email", "").strip().lower()
            if addr:
                done.add(addr)
        except Exception:
            pass
    return done


# ═══════════════════════════════════════════════════════
# 姓名 / 生日生成
# ═══════════════════════════════════════════════════════
_GIVEN_NAMES = [
    "Liam", "Noah", "Oliver", "James", "Elijah", "William", "Henry", "Lucas",
    "Benjamin", "Theodore", "Jack", "Levi", "Alexander", "Mason", "Ethan",
    "Daniel", "Jacob", "Michael", "Logan", "Jackson", "Sebastian", "Aiden",
    "Owen", "Samuel", "Ryan", "Nathan", "Carter", "Luke", "Jayden", "Dylan",
    "Caleb", "Isaac", "Connor", "Adrian", "Hunter", "Eli", "Thomas", "Aaron",
    "Olivia", "Emma", "Charlotte", "Amelia", "Sophia", "Isabella", "Mia",
    "Evelyn", "Harper", "Luna", "Camila", "Sofia", "Scarlett", "Elizabeth",
    "Eleanor", "Emily", "Chloe", "Mila", "Avery", "Riley", "Aria", "Layla",
    "Nora", "Lily", "Hannah", "Hazel", "Zoey", "Stella", "Aurora", "Natalie",
    "Emilia", "Zoe", "Lucy", "Lillian", "Addison", "Willow", "Ivy", "Violet",
]

_FAMILY_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Thompson", "White", "Harris", "Clark", "Lewis", "Robinson",
    "Walker", "Young", "Allen", "King", "Wright", "Hill", "Scott", "Green",
    "Adams", "Baker", "Nelson", "Carter", "Mitchell", "Roberts", "Turner",
    "Phillips", "Campbell", "Parker", "Evans", "Edwards", "Collins", "Stewart",
    "Morris", "Murphy", "Cook", "Rogers", "Morgan", "Cooper", "Peterson",
    "Reed", "Bailey", "Kelly", "Howard", "Ward", "Watson", "Brooks", "Bennett",
    "Gray", "Price", "Hughes", "Sanders", "Long", "Foster", "Powell", "Perry",
    "Russell", "Sullivan", "Bell", "Coleman", "Butler", "Henderson", "Barnes",
]


def random_name() -> str:
    """生成随机英文姓名"""
    return f"{random.choice(_GIVEN_NAMES)} {random.choice(_FAMILY_NAMES)}"


def random_birthday() -> str:
    """生成随机生日（18~40岁之间）"""
    y = random.randint(1986, 2006)
    m = random.randint(1, 12)
    d = random.randint(1, 28)
    return f"{y}-{m:02d}-{d:02d}"


# ═══════════════════════════════════════════════════════
# PKCE + OAuth 工具
# ═══════════════════════════════════════════════════════
def _urlsafe_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def create_pkce_pair() -> tuple[str, str]:
    """创建 PKCE code_verifier 和 code_challenge"""
    verifier = secrets.token_urlsafe(48)
    challenge = _urlsafe_b64(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def create_oauth_params() -> dict:
    """生成完整的 OAuth 参数集"""
    verifier, challenge = create_pkce_pair()
    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode({
        "client_id": OAI_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": LOCAL_REDIRECT_URI,
        "scope": "openid email profile offline_access",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    })
    return {
        "auth_url": f"{OAI_AUTH_URL}?{query}",
        "state": state,
        "verifier": verifier,
    }


def decode_jwt_payload(token: str) -> dict:
    """解码 JWT payload（不验证签名）"""
    try:
        payload = token.split(".")[1]
        padding = "=" * ((4 - len(payload) % 4) % 4)
        raw = base64.urlsafe_b64decode(payload + padding)
        return json.loads(raw)
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════
# Microsoft OAuth2 Token 刷新（用于 Outlook IMAP 认证）
# ═══════════════════════════════════════════════════════
_ms_token_cache = {}
_ms_cache_lock = threading.Lock()


def refresh_ms_token(account: MailAccount, timeout: int = 15) -> str:
    """刷新 Microsoft access token，结果缓存"""
    if not account.client_id or not account.refresh_token:
        raise RuntimeError("缺少 client_id 或 refresh_token")

    key = account.email.lower()
    with _ms_cache_lock:
        cached = _ms_token_cache.get(key)
        if cached and time.time() < cached[1]:
            return cached[0]

    body = urllib.parse.urlencode({
        "client_id": account.client_id,
        "refresh_token": account.refresh_token,
        "grant_type": "refresh_token",
        "redirect_uri": "https://login.live.com/oauth20_desktop.srf",
    }).encode()
    req = urllib.request.Request("https://login.live.com/oauth20_token.srf", data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except HTTPError as e:
        raise RuntimeError(f"MS OAuth 刷新失败: {e.code}") from e

    token = data.get("access_token")
    if not token:
        raise RuntimeError("MS OAuth 响应无 access_token")
    ttl = int(data.get("expires_in", 3600))
    with _ms_cache_lock:
        _ms_token_cache[key] = (token, time.time() + ttl - 120)
    return token


def _build_xoauth2(email_addr: str, token: str) -> bytes:
    return f"user={email_addr}\x01auth=Bearer {token}\x01\x01".encode()


# ═══════════════════════════════════════════════════════
# IMAP 邮件获取
# ═══════════════════════════════════════════════════════
_RE_CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


class OutlookIMAP:
    """Outlook IMAP 连接，支持 XOAUTH2 和密码认证"""

    def __init__(self, account: MailAccount, host="outlook.office365.com", port=993):
        self.account = account
        self.host = host
        self.port = port
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    def connect(self):
        self._conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=20)
        # 优先 XOAUTH2
        if self.account.client_id and self.account.refresh_token:
            try:
                token = refresh_ms_token(self.account)
                self._conn.authenticate("XOAUTH2",
                    lambda _: _build_xoauth2(self.account.email, token))
                return
            except Exception:
                pass
        # 回退密码
        self._conn.login(self.account.email, self.account.password)

    def _ensure(self):
        if self._conn:
            try:
                self._conn.noop()
                return
            except Exception:
                self.close()
        self.connect()

    def get_recent_mails(self, count=20, only_unseen=True) -> list[dict]:
        """获取最近的邮件（解析为字典列表）"""
        self._ensure()
        flag = "UNSEEN" if only_unseen else "ALL"
        self._conn.select("INBOX", readonly=True)
        _, data = self._conn.search(None, flag)
        if not data or not data[0]:
            return []
        ids = data[0].split()[-count:]
        result = []
        for mid in reversed(ids):
            _, payload = self._conn.fetch(mid, "(RFC822)")
            if not payload:
                continue
            raw = b""
            for part in payload:
                if isinstance(part, tuple) and len(part) > 1:
                    raw = part[1]
                    break
            if raw:
                result.append(self._parse(raw))
        return result

    @staticmethod
    def _parse(raw: bytes) -> dict:
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        msg = email_module.message_from_bytes(raw)
        subject = OutlookIMAP._decode_header(msg.get("Subject", ""))
        sender = OutlookIMAP._decode_header(msg.get("From", ""))
        date = OutlookIMAP._decode_header(msg.get("Date", ""))
        to = OutlookIMAP._decode_header(msg.get("To", ""))
        delivered_to = OutlookIMAP._decode_header(msg.get("Delivered-To", ""))
        x_original_to = OutlookIMAP._decode_header(msg.get("X-Original-To", ""))
        body = OutlookIMAP._extract_body(msg)
        return {
            "subject": subject, "from": sender, "date": date, "body": body,
            "to": to, "delivered_to": delivered_to, "x_original_to": x_original_to,
        }

    @staticmethod
    def _decode_header(val):
        if not val:
            return ""
        parts = []
        for chunk, enc in decode_header(val):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(enc or "utf-8", errors="replace"))
            else:
                parts.append(chunk)
        return "".join(parts).strip()

    @staticmethod
    def _extract_body(msg) -> str:
        texts = []
        parts = msg.walk() if msg.is_multipart() else [msg]
        for part in parts:
            ct = part.get_content_type()
            if ct not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                t = payload.decode(charset, errors="replace")
            except LookupError:
                t = payload.decode("utf-8", errors="replace")
            if "<html" in t.lower():
                t = re.sub(r"<[^>]+>", " ", t)
            texts.append(t)
        return re.sub(r"\s+", " ", html_module.unescape(" ".join(texts))).strip()

    def close(self):
        if self._conn:
            try: self._conn.close()
            except: pass
            try: self._conn.logout()
            except: pass
            self._conn = None

    def __enter__(self): return self
    def __exit__(self, *a): self.close()


class DomainIMAP:
    """域名邮箱 IMAP（catch-all），用于从统一邮箱收取所有子地址的验证码"""

    def __init__(self, host: str, port: int, user: str, password: str):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    def connect(self):
        self._conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=20)
        self._conn.login(self.user, self.password)

    def _ensure(self):
        if self._conn:
            try:
                self._conn.noop()
                return
            except Exception:
                self.close()
        self.connect()

    def get_recent_mails(self, count=20, only_unseen=True) -> list[dict]:
        """获取最近的邮件（复用 OutlookIMAP 的解析逻辑）"""
        self._ensure()
        flag = "UNSEEN" if only_unseen else "ALL"
        self._conn.select("INBOX", readonly=True)
        _, data = self._conn.search(None, flag)
        if not data or not data[0]:
            return []
        ids = data[0].split()[-count:]
        result = []
        for mid in reversed(ids):
            _, payload = self._conn.fetch(mid, "(RFC822)")
            if not payload:
                continue
            raw = b""
            for part in payload:
                if isinstance(part, tuple) and len(part) > 1:
                    raw = part[1]
                    break
            if raw:
                result.append(OutlookIMAP._parse(raw))
        return result

    def close(self):
        if self._conn:
            try: self._conn.close()
            except: pass
            try: self._conn.logout()
            except: pass
            self._conn = None

    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def _is_oai_mail(mail: dict) -> bool:
    """判断是否为 OpenAI 相关邮件"""
    combined = f"{mail.get('from','')} {mail.get('subject','')} {mail.get('body','')}".lower()
    return any(kw in combined for kw in ("openai", "chatgpt", "verification", "验证码"))


# 全局 IMAP 并发限制（Outlook 限流保护）
_imap_semaphore = threading.Semaphore(10)


# ═══════════════════════════════════════════════════════
# 域名邮箱共享轮询器
# ═══════════════════════════════════════════════════════
class DomainMailHub:
    """
    域名邮箱统一轮询器：1 个 IMAP 连接服务 N 个 worker。
    各 worker 注册自己的 email，hub 后台轮询邮件并按收件人分发验证码。
    """
    _instances: dict[str, "DomainMailHub"] = {}
    _instances_lock = threading.Lock()

    @classmethod
    def get_or_create(cls, domain_mail: dict) -> "DomainMailHub":
        key = f"{domain_mail['host']}:{domain_mail.get('port',993)}:{domain_mail['user']}"
        with cls._instances_lock:
            if key not in cls._instances or not cls._instances[key]._running:
                hub = cls(domain_mail)
                hub.start()
                cls._instances[key] = hub
            return cls._instances[key]

    def __init__(self, domain_mail: dict):
        self._config = domain_mail
        self._running = False
        self._lock = threading.Lock()
        # email -> queue of (code, source) tuples
        self._waiters: dict[str, list] = {}
        # email -> set of used codes
        self._delivered: dict[str, set] = {}
        self._thread: Optional[threading.Thread] = None
        self._ref_count = 0

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def register(self, email: str):
        """Worker 注册等待验证码"""
        email_lower = email.lower()
        with self._lock:
            self._ref_count += 1
            if email_lower not in self._waiters:
                self._waiters[email_lower] = []
            if email_lower not in self._delivered:
                self._delivered[email_lower] = set()

    def unregister(self, email: str):
        """Worker 完成，取消注册"""
        with self._lock:
            self._ref_count -= 1
            if self._ref_count <= 0:
                self._ref_count = 0

    def wait_code(self, email: str, timeout: int, used_codes: set,
                  otp_sent_at: float, cancel_fn=None, resend_fn=None) -> str:
        """阻塞等待验证码，直到收到或超时"""
        email_lower = email.lower()
        min_ts = (otp_sent_at - 60) if otp_sent_at else 0
        start = time.time()
        last_resend = 0.0

        while time.time() - start < timeout:
            if cancel_fn and cancel_fn():
                raise InterruptedError("用户取消")

            # 检查是否有分发的验证码
            with self._lock:
                queue = self._waiters.get(email_lower, [])
                while queue:
                    code, source, mail_ts = queue.pop(0)
                    if code in used_codes:
                        continue
                    if min_ts > 0 and mail_ts and mail_ts < min_ts:
                        continue
                    used_codes.add(code)
                    elapsed = int(time.time() - start)
                    log.info(f"    ✅ 验证码: {code} (耗时 {elapsed}s, 来源: {source}, 共享轮询)")
                    return code

            # 定时重发 OTP
            elapsed_now = time.time() - start
            if resend_fn and elapsed_now > 20 and (elapsed_now - last_resend) > OTP_RESEND_INTERVAL:
                try:
                    resend_fn()
                    last_resend = elapsed_now
                    log.info("    🔄 已重发 OTP")
                except Exception:
                    pass

            # 等待
            end = time.time() + 2
            while time.time() < end:
                if cancel_fn and cancel_fn():
                    raise InterruptedError("用户取消")
                time.sleep(0.3)

        raise TimeoutError(f"验证码超时 ({timeout}s)")

    def _poll_loop(self):
        """后台轮询线程"""
        imap = None
        fails = 0
        poll_idx = 0

        while self._running:
            try:
                # 没有 waiter 时休眠
                with self._lock:
                    if self._ref_count <= 0:
                        time.sleep(1)
                        continue

                # 连接/重连
                if imap is None:
                    imap = DomainIMAP(
                        host=self._config["host"],
                        port=int(self._config.get("port", 993)),
                        user=self._config["user"],
                        password=self._config["pass"],
                    )
                    imap.connect()
                    log.info("    📡 域名邮箱 Hub: IMAP 已连接")

                mails = imap.get_recent_mails(count=30, only_unseen=(poll_idx < 3))
                fails = 0
                poll_idx += 1

                for m in mails:
                    if not _is_oai_mail(m):
                        continue

                    mail_ts = _parse_email_date(m.get("date", ""))

                    # 提取验证码
                    subject = m.get("subject", "")
                    code = None
                    source = "subject"
                    subj_match = _RE_CODE.search(subject)
                    if subj_match:
                        code = subj_match.group(1)
                    else:
                        body = m.get("body", "")
                        precise = re.search(r'(?:code\s+is|验证码)\s*(\d{6})', body, re.IGNORECASE)
                        if precise:
                            code = precise.group(1)
                            source = "body"

                    if not code:
                        continue

                    # 匹配收件人
                    recipients = set()
                    for field in ("to", "delivered_to", "x_original_to"):
                        val = m.get(field, "").lower()
                        if val:
                            # 提取邮箱地址
                            addrs = re.findall(r'[\w.+-]+@[\w.-]+', val)
                            recipients.update(addrs)

                    # 分发给匹配的 waiter
                    with self._lock:
                        for email_lower, queue in self._waiters.items():
                            if email_lower in recipients:
                                # 避免重复分发
                                delivered = self._delivered.get(email_lower, set())
                                if code not in delivered:
                                    delivered.add(code)
                                    queue.append((code, source, mail_ts))

                # 轮询间隔
                time.sleep(3)

            except Exception as e:
                fails += 1
                log.warning(f"    📡 域名邮箱 Hub 出错({fails}): {e}")
                if imap:
                    try:
                        imap.close()
                    except Exception:
                        pass
                    imap = None
                time.sleep(2)

        # 清理
        if imap:
            try:
                imap.close()
            except Exception:
                pass


def _parse_email_date(date_str: str) -> Optional[float]:
    """解析邮件 Date 头为 Unix 时间戳"""
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str).timestamp()
    except Exception:
        return None


GO_IMAP_SERVICE = "http://localhost:8899"


def _try_go_imap_service(
    email: str,
    timeout: int,
    cancel_fn: Optional[Callable] = None,
    resend_fn: Optional[Callable] = None,
    otp_sent_at: Optional[float] = None,
) -> Optional[str]:
    """尝试从 Go IMAP 服务获取验证码（非阻塞，服务不可用时返回 None）"""
    import urllib.request

    # 先检测服务是否可用
    try:
        test_req = urllib.request.Request(f"{GO_IMAP_SERVICE}/api/status")
        with urllib.request.urlopen(test_req, timeout=2) as resp:
            if resp.status != 200:
                return None
    except Exception:
        return None

    log.info(f"    📡 使用 Go IMAP 服务获取验证码...")

    start = time.time()
    last_resend = 0.0
    wait_per_request = min(15, timeout)  # 每次查询最多阻塞 15 秒

    while time.time() - start < timeout:
        if cancel_fn and cancel_fn():
            raise InterruptedError("用户取消")

        remaining = int(timeout - (time.time() - start))
        wait = min(wait_per_request, remaining)
        if wait <= 0:
            break

        try:
            url = f"{GO_IMAP_SERVICE}/api/code?email={urllib.parse.quote(email)}&wait={wait}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=wait + 5) as resp:
                data = json.loads(resp.read())
                code = data.get("code", "")
                if code and len(code) == 6:
                    # 消费验证码
                    consume_body = json.dumps({"email": email, "code": code}).encode()
                    consume_req = urllib.request.Request(
                        f"{GO_IMAP_SERVICE}/api/consume",
                        data=consume_body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    try:
                        urllib.request.urlopen(consume_req, timeout=3)
                    except Exception:
                        pass
                    elapsed = int(time.time() - start)
                    log.info(f"    ✅ 验证码: {code} (耗时 {elapsed}s, Go IMAP 服务)")
                    return code
        except Exception as e:
            log.warning(f"    ⚠ Go IMAP 服务查询失败: {e}")
            return None  # 服务不可用，回退

        # 重发 OTP
        elapsed_now = time.time() - start
        if resend_fn and elapsed_now > 20 and (elapsed_now - last_resend) > OTP_RESEND_INTERVAL:
            try:
                resend_fn()
                last_resend = elapsed_now
                log.info("    🔄 已重发 OTP")
            except Exception:
                pass

    raise TimeoutError(f"验证码超时 ({timeout}s)")


def poll_verification_code(
    account: MailAccount,
    timeout: int = IMAP_POLL_TIMEOUT,
    used_codes: Optional[set] = None,
    resend_fn: Optional[Callable] = None,
    otp_sent_at: Optional[float] = None,
    cancel_fn: Optional[Callable] = None,
    domain_mail: Optional[dict] = None,
    is_freemail: bool = False,
    freemail_worker_domain: str = "",
    freemail_token: str = "",
) -> str:
    """轮询邮箱获取 OpenAI 6 位验证码
    
    otp_sent_at: OTP 发送时的 Unix 时间戳，只接受此时间之后的邮件
    domain_mail: 域名邮箱配置 {host, port, user, pass}，使用 catch-all 模式
    """
    is_domain = domain_mail is not None
    mode_label = "域名邮箱" if is_domain else "Outlook"
    if is_freemail:
        mode_label = "FreeMail"
    log.info(f"    📧 等待验证码 ({account.email}, {mode_label})...")
    used = used_codes or set()
    #Freemail
    if is_freemail:
        worker_domain = (freemail_worker_domain or "").strip()
        token = (freemail_token or "").strip()
        if not worker_domain or not token:
            raise ValueError("❌ 缺少 FreeMail 配置: worker_domain 或 token")
        BASE_URL = worker_domain if worker_domain.startswith(("http://", "https://")) else f"https://{worker_domain}"
        HEADERS = {"Authorization": f"Bearer {token}"}
        log.info(f"    📡 使用 FreeMail 服务获取验证码...")
        start_time = time.time()
        last_resend = 0.0
        interval = 2
        while time.time() - start_time < timeout:
            if cancel_fn and cancel_fn():
                raise InterruptedError("用户取消")
            try:
                resp = requests.get(
                    f"{BASE_URL}/api/emails",
                    params={"mailbox": account.email},
                    headers=HEADERS,
                    timeout=10
                )
                if resp.status_code == 200:
                    emails=resp.json()
                    if emails and len(emails)>0:
                        first_email=emails[0]
                        log.info(f"    📡 FreeMail 收到邮件: {first_email.get('subject','')}")
                        subject=first_email.get("subject","")
                        match = re.search(r'([0-9]{6})$', subject)
                        if match:
                            code=match.group(1)
                            if code not in used:
                                used.add(code)
                                elapsed = int(time.time() - start_time)
                                log.info(f"    ✅ 验证码: {code} (耗时 {elapsed}s, FreeMail 服务)")
                                return code
                else:
                    log.warning(f"    ⚠ FreeMail 服务响应异常: {resp.status_code} {resp.text}")
            except Exception as e:
                log.warning(f"    ⚠ FreeMail 服务查询失败: {e}")

            elapsed_now = time.time() - start_time
            if resend_fn and elapsed_now > 20 and (elapsed_now - last_resend) > OTP_RESEND_INTERVAL:
                try:
                    resend_fn()
                    last_resend = elapsed_now
                except Exception:
                    pass

            time.sleep(interval)

        raise TimeoutError(f"验证码超时 ({timeout}s)")

    # ★ 域名邮箱快速路径
    if is_domain:
        # 优先使用 Go IMAP 服务（极速版，端口 8900）
        code = _try_go_imap_service(account.email, timeout, cancel_fn, resend_fn, otp_sent_at)
        if code:
            return code

        # 回退到 Python 内置 Hub
        log.info("    📡 Go IMAP 服务不可用，使用内置 Hub")
        hub = DomainMailHub.get_or_create(domain_mail)
        hub.register(account.email)
        try:
            return hub.wait_code(
                account.email, timeout, used,
                otp_sent_at=otp_sent_at or 0,
                cancel_fn=cancel_fn,
                resend_fn=resend_fn,
            )
        finally:
            hub.unregister(account.email)

    # ★ Outlook 模式：每个账号独立 IMAP 连接
    start = time.time()
    email_lower = account.email.lower()
    # 如果有 otp_sent_at，只接受该时间 60 秒前之后的邮件（留出时钟偏差）
    min_ts = (otp_sent_at - 60) if otp_sent_at else 0
    intervals = [3, 4, 5, 6, 8, 10]
    idx = 0
    last_resend = 0.0
    imap = None
    imap_fails = 0

    def _cancelled():
        return cancel_fn and cancel_fn()

    def _interruptible_sleep(seconds):
        """可中断的 sleep，每 0.5s 检查一次取消"""
        end = time.time() + seconds
        while time.time() < end:
            if _cancelled():
                raise InterruptedError("用户取消")
            time.sleep(min(0.5, max(0, end - time.time())))

    def _mail_is_for_recipient(m, target_email):
        """检查邮件是否发给目标收件人（域名邮箱模式用）"""
        check_fields = [
            m.get("to", ""),
            m.get("delivered_to", ""),
            m.get("x_original_to", ""),
        ]
        for field in check_fields:
            if target_email in field.lower():
                return True
        # 正文兜底
        body = m.get("body", "")
        if target_email in body.lower():
            return True
        return False

    def _connect_imap():
        """建立 IMAP 连接（带信号量限制并发）"""
        nonlocal imap
        if imap:
            try:
                imap.close()
            except Exception:
                pass
        _imap_semaphore.acquire()
        try:
            if is_domain:
                imap = DomainIMAP(
                    host=domain_mail["host"],
                    port=int(domain_mail.get("port", 993)),
                    user=domain_mail["user"],
                    password=domain_mail["pass"],
                )
                imap.connect()
            else:
                imap = OutlookIMAP(account)
                imap.connect()
        except Exception:
            _imap_semaphore.release()
            raise

    def _close_imap():
        nonlocal imap
        if imap:
            try:
                imap.close()
            except Exception:
                pass
            imap = None
            _imap_semaphore.release()

    try:
        _connect_imap()

        while time.time() - start < timeout:
            if _cancelled():
                raise InterruptedError("用户取消")

            try:
                mails = imap.get_recent_mails(count=20, only_unseen=(idx < 2))
                imap_fails = 0  # 重置失败计数

                for m in mails:
                    if not _is_oai_mail(m):
                        continue

                    # 域名邮箱模式：按收件人过滤
                    if is_domain and not _mail_is_for_recipient(m, email_lower):
                        continue

                    # 时间戳过滤：只接受 OTP 发送之后的邮件
                    if min_ts > 0:
                        mail_ts = _parse_email_date(m.get("date", ""))
                        if mail_ts and mail_ts < min_ts:
                            continue  # 旧邮件，跳过

                    # 优先从 subject 提取（最可靠: "Your OpenAI code is 123456"）
                    subject = m.get("subject", "")
                    subj_match = _RE_CODE.search(subject)
                    if subj_match and subj_match.group(1) not in used:
                        code = subj_match.group(1)
                        used.add(code)
                        elapsed = int(time.time() - start)
                        log.info(f"    ✅ 验证码: {code} (耗时 {elapsed}s, 来源: subject)")
                        return code

                    # 仅当 subject 无匹配时，从 body 用精确模式
                    if not subj_match:
                        body = m.get("body", "")
                        precise = re.search(r'(?:code\s+is|验证码)\s*(\d{6})', body, re.IGNORECASE)
                        if precise and precise.group(1) not in used:
                            code = precise.group(1)
                            used.add(code)
                            elapsed = int(time.time() - start)
                            log.info(f"    ✅ 验证码: {code} (耗时 {elapsed}s, 来源: body)")
                            return code

            except InterruptedError:
                raise
            except Exception as e:
                imap_fails += 1
                log.warning(f"    IMAP 出错({imap_fails}): {e}")
                # 连续失败 2 次，尝试重连
                if imap_fails >= 2:
                    log.info("    🔄 重连 IMAP...")
                    _close_imap()
                    _interruptible_sleep(2)
                    try:
                        _connect_imap()
                        imap_fails = 0
                        log.info("    ✅ IMAP 重连成功")
                    except Exception as e2:
                        log.warning(f"    IMAP 重连失败: {e2}")

            # 定时重发 OTP
            elapsed_now = time.time() - start
            if resend_fn and elapsed_now > 20 and (elapsed_now - last_resend) > OTP_RESEND_INTERVAL:
                try:
                    resend_fn()
                    last_resend = elapsed_now
                    log.info("    🔄 已重发 OTP")
                except Exception:
                    pass

            wait = intervals[min(idx, len(intervals) - 1)]
            idx += 1
            _interruptible_sleep(wait)

        raise TimeoutError(f"验证码超时 ({timeout}s)")
    finally:
        _close_imap()


# ═══════════════════════════════════════════════════════
# 浏览器指纹伪装
# ═══════════════════════════════════════════════════════
# curl_cffi 可模拟的浏览器身份（会自动生成对应的 TLS 指纹、HTTP/2 设置等）
_BROWSER_PROFILES = [
    "chrome120", "chrome123", "chrome124", "chrome131",
    "chrome133a", "chrome136",
    "edge99", "edge101",
    "safari15_3", "safari15_5", "safari17_0",
]

# 对应不同浏览器的 Accept-Language 头
_ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.9,zh-CN;q=0.8",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "en-US,en;q=0.8",
    "en,en-US;q=0.9",
    "en-US,en;q=0.9,de;q=0.7",
    "en-US,en;q=0.9,ja;q=0.7",
    "en-US,en;q=0.9,fr;q=0.7",
]


def _pick_fingerprint() -> tuple[str, dict]:
    """随机选择浏览器身份和对应请求头"""
    profile = random.choice(_BROWSER_PROFILES)
    lang = random.choice(_ACCEPT_LANGUAGES)
    headers = {
        "Accept-Language": lang,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    return profile, headers


# ═══════════════════════════════════════════════════════
# HTTP 会话（基于 curl_cffi，自带浏览器 TLS 指纹）
# ═══════════════════════════════════════════════════════
class APISession:
    """基于 curl_cffi 的 HTTP 会话，内置浏览器指纹伪装"""

    def __init__(self, proxy: str = ""):
        profile, fp_headers = _pick_fingerprint()
        proxies = {"http": proxy, "https": proxy} if proxy else None
        self._session = cffi_requests.Session(proxies=proxies, impersonate=profile)
        self._session.headers.update(fp_headers)
        self._profile = profile
        log.info(f"    🎭 浏览器指纹: {profile}")

    def get(self, url: str, **kwargs) -> "APIResponse":
        resp = self._session.get(url, timeout=30, **kwargs)
        return APIResponse(resp.status_code, resp.text, dict(resp.headers))

    def post_json(self, url: str, data: dict, headers: Optional[dict] = None) -> "APIResponse":
        hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        resp = self._session.post(url, data=json.dumps(data), headers=hdrs, timeout=30)
        return APIResponse(resp.status_code, resp.text, dict(resp.headers))

    def post_form(self, url: str, data: dict) -> "APIResponse":
        hdrs = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        resp = self._session.post(url, data=urllib.parse.urlencode(data), headers=hdrs, timeout=30)
        return APIResponse(resp.status_code, resp.text, dict(resp.headers))

    def get_cookie(self, name: str) -> Optional[str]:
        return self._session.cookies.get(name)

    def follow_redirects(self, url: str, max_hops: int = 12) -> Optional[str]:
        """跟随 302 重定向链，返回包含 localhost 的回调 URL"""
        for _ in range(max_hops):
            resp = self._session.get(url, allow_redirects=False, timeout=30)
            location = resp.headers.get("Location")
            if not location:
                return None
            if "localhost" in location and "/auth/callback" in location:
                return location
            url = location
        return None

    def close(self):
        self._session.close()

    def __enter__(self): return self
    def __exit__(self, *a): self.close()


@dataclass
class APIResponse:
    status: int
    text: str
    headers: dict

    def json(self) -> dict:
        return json.loads(self.text)

    def ok(self) -> bool:
        return 200 <= self.status < 300


# ═══════════════════════════════════════════════════════
# 核心注册/登录流程
# ═══════════════════════════════════════════════════════
def register_account(
    mail_account: MailAccount,
    proxy: str = "",
    used_codes: Optional[set] = None,
    mode: str = "register",
    cancel_fn: Optional[Callable] = None,
    domain_mail: Optional[dict] = None,
    freemail_worker_domain: str = "",
    freemail_token: str = "",
) -> dict:
    """
    通过 API 注册或登录 OpenAI 账号。
    mode: "register" = 新注册, "login" = 已有账号登录刷新 token
    返回包含 token 信息的字典。
    """
    email_addr = mail_account.email
    codes = used_codes or set()
    is_login = (mode == "login")
    mode_label = "登录" if is_login else "注册"

    def _check_cancel():
        if cancel_fn and cancel_fn():
            raise InterruptedError("用户取消")

    def _sleep(lo, hi):
        """可中断的随机 sleep"""
        end = time.time() + random.uniform(lo, hi)
        while time.time() < end:
            _check_cancel()
            time.sleep(min(0.3, max(0, end - time.time())))

    with APISession(proxy) as http:
        # --- 1. 发起 OAuth 授权 ---
        _check_cancel()
        oauth = create_oauth_params()
        log.info(f"  [1] 发起 OAuth ({mode_label})...")
        resp = http.get(oauth["auth_url"])
        log.info(f"      状态: {resp.status}")

        device_id = http.get_cookie("oai-did") or ""
        if device_id:
            log.info(f"      设备ID: {device_id[:16]}...")

        _sleep(0.8, 2.0)

        # --- 2. 获取 Sentinel 反机器人令牌 ---
        _check_cancel()
        log.info(f"  [2] 获取 Sentinel token...")
        sentinel_body = {"p": "", "id": device_id, "flow": "authorize_continue"}
        sentinel_resp = http.post_json(
            OAI_SENTINEL_URL, sentinel_body,
            headers={
                "Origin": "https://sentinel.openai.com",
                "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
            }
        )
        if not sentinel_resp.ok():
            raise RuntimeError(f"Sentinel 失败: {sentinel_resp.status} {sentinel_resp.text[:200]}")
        sentinel_token = sentinel_resp.json()["token"]
        sentinel_header = json.dumps({
            "p": "", "t": "", "c": sentinel_token,
            "id": device_id, "flow": "authorize_continue",
        })
        log.info(f"      OK")

        _sleep(0.5, 1.5)

        # --- 3. 提交邮箱（始终用 signup 流，登录模式在步骤 7 跳过创建）---
        _check_cancel()
        # 记录时间戳——已注册账号在此步骤后 OTP 就会自动发送
        otp_sent_at = time.time()
        log.info(f"  [3] 提交邮箱: {email_addr} ({mode_label})")
        signup_resp = http.post_json(
            OAI_SIGNUP_URL,
            {"username": {"value": email_addr, "kind": "email"}, "screen_hint": "signup"},
            headers={
                "Referer": "https://auth.openai.com/create-account",
                "openai-sentinel-token": sentinel_header,
            },
        )
        if not signup_resp.ok():
            raise RuntimeError(f"提交邮箱失败: {signup_resp.status} {signup_resp.text[:300]}")
        log.info(f"      OK")

        # 解析步骤3响应，判断账号状态
        try:
            step3_data = signup_resp.json()
            page_type = step3_data.get("page", {}).get("type", "")
        except Exception:
            step3_data = {}
            page_type = ""
        log.info(f"      页面类型: {page_type}")

        _sleep(0.5, 1.5)

        name = ""
        # 已注册账号：步骤3返回 email_otp_verification → OTP 已自动发送
        is_existing_account = (page_type == "email_otp_verification")

        _check_cancel()
        if is_existing_account:
            # 已注册账号：OTP 在步骤3提交邮箱时已自动发送
            log.info(f"  [4] 跳过发送 OTP（服务器已自动发送）")
        else:
            # --- 4. 请求发送 OTP 验证码（新账号需要手动触发）---
            try:
                delete_mail(
                    mail_account,
                    freemail_worker_domain=freemail_worker_domain,
                    freemail_token=freemail_token,
                )
            except Exception as e:
                log.warning(f"  💾 删除历史邮件失败，继续发送 OTP: {e}")

            otp_sent_at = time.time()
            log.info(f"  [4] 发送 OTP...")
            otp_resp = http.post_json(
                OAI_SEND_OTP_URL, {},
                headers={"Referer": "https://auth.openai.com/create-account/password"},
            )
            if not otp_resp.ok():
                raise RuntimeError(f"发送 OTP 失败: {otp_resp.status} {otp_resp.text[:300]}")
            log.info(f"      OK，验证码已发送到 {email_addr}")

        # --- 5. 通过 IMAP 获取验证码 ---
        def _resend():
            r = http.post_json(OAI_SEND_OTP_URL, {},
                headers={"Referer": "https://auth.openai.com/email-verification"})
            return r.ok()

        code = poll_verification_code(
            mail_account, used_codes=codes,
            resend_fn=_resend,
            otp_sent_at=otp_sent_at,
            cancel_fn=cancel_fn,
            domain_mail=domain_mail,
            is_freemail=mail_account.is_freemail,
            freemail_worker_domain=freemail_worker_domain,
            freemail_token=freemail_token,
        )

        _check_cancel()
        _sleep(0.3, 1.0)

        # --- 6. 验证 OTP ---
        _check_cancel()
        log.info(f"  [6] 验证 OTP: {code}")
        verify_resp = http.post_json(
            OAI_VERIFY_OTP_URL, {"code": code},
            headers={"Referer": "https://auth.openai.com/email-verification"},
        )
        if not verify_resp.ok():
            raise RuntimeError(f"OTP 验证失败: {verify_resp.status} {verify_resp.text[:300]}")
        log.info(f"      OK")

        _sleep(0.5, 1.5)

        # --- 7. 创建账号（仅新注册时，已注册账号跳过）---
        _check_cancel()
        if is_existing_account or is_login:
            log.info(f"  [7] 跳过（账号已存在）")
        else:
            name = random_name()
            birthday = random_birthday()
            log.info(f"  [7] 创建账号: {name}, {birthday}")
            create_resp = http.post_json(
                OAI_CREATE_URL,
                {"name": name, "birthdate": birthday},
                headers={"Referer": "https://auth.openai.com/about-you"},
            )
            if not create_resp.ok():
                raise RuntimeError(f"创建账号失败: {create_resp.status} {create_resp.text[:300]}")
            log.info(f"      OK")
            _sleep(0.5, 1.5)

        # --- 8. 选择 Workspace ---
        auth_cookie = http.get_cookie("oai-client-auth-session")
        if not auth_cookie:
            raise RuntimeError("未获取到 oai-client-auth-session cookie")

        # 解析 cookie 获取 workspace_id
        try:
            cookie_b64 = auth_cookie.split(".")[0]
            padding = "=" * ((4 - len(cookie_b64) % 4) % 4)
            cookie_data = json.loads(base64.b64decode(cookie_b64 + padding))
            workspaces = cookie_data.get("workspaces", [])
            workspace_id = workspaces[0]["id"] if workspaces else None
        except Exception as e:
            raise RuntimeError(f"解析 workspace 失败: {e}")

        if not workspace_id:
            raise RuntimeError("未找到 workspace_id")

        log.info(f"  [8] 选择 Workspace: {workspace_id[:20]}...")
        select_resp = http.post_json(
            OAI_WORKSPACE_URL,
            {"workspace_id": workspace_id},
            headers={"Referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"},
        )
        if not select_resp.ok():
            raise RuntimeError(f"选择 workspace 失败: {select_resp.status}")

        continue_url = select_resp.json().get("continue_url")
        if not continue_url:
            raise RuntimeError("未获取到 continue_url")

        # --- 9. 跟随重定向，获取回调并兑换 token ---
        log.info(f"  [9] 跟随重定向获取 Token...")
        callback_url = http.follow_redirects(continue_url)
        if not callback_url:
            raise RuntimeError("重定向失败，未获取到回调 URL")

        # 解析回调 URL 中的 code
        parsed = urllib.parse.urlparse(callback_url)
        query = urllib.parse.parse_qs(parsed.query)
        auth_code = query.get("code", [""])[0]
        returned_state = query.get("state", [""])[0]

        if not auth_code:
            raise RuntimeError("回调 URL 缺少 code")
        if returned_state != oauth["state"]:
            raise RuntimeError("State 不匹配")

        # 兑换 token
        token_resp = http.post_form(OAI_TOKEN_URL, {
            "grant_type": "authorization_code",
            "client_id": OAI_CLIENT_ID,
            "code": auth_code,
            "redirect_uri": LOCAL_REDIRECT_URI,
            "code_verifier": oauth["verifier"],
        })
        if not token_resp.ok():
            raise RuntimeError(f"Token 兑换失败: {token_resp.status} {token_resp.text[:300]}")

        token_data = token_resp.json()

        # 解析 id_token 获取额外信息
        claims = decode_jwt_payload(token_data.get("id_token", ""))
        auth_claims = claims.get("https://api.openai.com/auth", {})

        now = int(time.time())
        result = {
            "email": email_addr,
            "type": "codex",
            "name": name or claims.get("name", ""),
            "access_token": token_data.get("access_token", ""),
            "refresh_token": token_data.get("refresh_token", ""),
            "id_token": token_data.get("id_token", ""),
            "account_id": auth_claims.get("chatgpt_account_id", ""),
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(now + int(token_data.get("expires_in", 0)))),
            "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "mode": mode,
        }

        log.info(f"  🎉 {mode_label}成功！")
        return result

def delete_mail(
    account: MailAccount,
    freemail_worker_domain: str = "",
    freemail_token: str = ""
):
    if not account.is_freemail:
        log.info("非freemail账号，跳过删除")
        return
    worker_domain = (freemail_worker_domain or "").strip()
    token = (freemail_token or "").strip()
    if not worker_domain or not token:
        log.warning("配置缺失")
        return
    BASE_URL = worker_domain if worker_domain.startswith(("http://", "https://")) else f"https://{worker_domain}"
    HEADERS = {"Authorization": f"Bearer {token}"}
    resp = requests.delete(
                    f"{BASE_URL}/api/emails",
                    params={"mailbox": account.email},
                    headers=HEADERS,
                    timeout=10
                )
    if resp.status_code == 200:
        result=resp.json()
        if result['success']:
            log.info(f"  💾 已删除邮箱{account.email}中的{result['deletedCount']}封邮件")
        else:
            log.warning("  💾 删除失败")
    else:
        log.warning(f"  💾 删除请求失败: {resp.status_code}")

# ═══════════════════════════════════════════════════════
# 单账号注册（带重试）
# ═══════════════════════════════════════════════════════
def _do_one(
    account: MailAccount,
    idx: int,
    total: int,
    proxy: str,
    stats: dict,
    lock: threading.Lock,
    delay: float = 0,
):
    """单个账号注册任务（线程安全）"""
    if delay > 0:
        time.sleep(delay)


    start_t = time.time()

    used = set()
    log.info(f"\n{'─'*50}")
    log.info(f"[{idx}/{total}] {account.email}")
    log.info(f"{'─'*50}")

    ok = False
    for attempt in range(1, MAX_RETRY_PER_ACCOUNT + 1):
        if attempt > 1:
            log.info(f"  重试 #{attempt}...")
            time.sleep(random.uniform(2, 5))
        try:
            result = register_account(account, proxy, used)
            elapsed = round(time.time() - start_t, 1)
            result["elapsed_seconds"] = elapsed

            # 保存结果
            os.makedirs(RESULTS_DIR, exist_ok=True)
            fpath = os.path.join(RESULTS_DIR, f"{account.email}.json")
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)

            with lock:
                stats["ok"] += 1

            log.info(f"  💾 已保存: {fpath} ({elapsed}s)")
            ok = True
            break

        except Exception as e:
            log.warning(f"  ❌ 尝试 {attempt} 失败: {type(e).__name__}: {str(e)[:150]}")

    if not ok:
        with lock:
            stats["fail"] += 1



# ═══════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="OpenAI 协议注册（纯 API 版）")
    parser.add_argument("--accounts", default=DEFAULT_ACCOUNTS_FILE, help="账号文件路径")
    parser.add_argument("--proxy", default="", help="HTTP 代理 (如 http://ip:port)")
    parser.add_argument("--workers", type=int, default=1, help="并发线程数")
    parser.add_argument("--limit", type=int, default=99999, help="最多注册数量")
    args = parser.parse_args()

    log.info("=" * 55)
    log.info("  OpenAI 协议注册 v1.0")
    log.info("=" * 55)

    accounts = load_accounts_file(args.accounts)
    log.info(f"📂 账号文件: {args.accounts}")
    log.info(f"📧 总数: {len(accounts)}")

    done = get_finished_emails(RESULTS_DIR)
    pending = [a for a in accounts if a.email.lower() not in done]
    log.info(f"✅ 已完成: {len(done)}, 待注册: {len(pending)}")

    if not pending:
        log.info("没有待注册的账号！")
        return

    batch = pending[:args.limit]
    total = len(batch)
    log.info(f"🚀 本次注册: {total} 个 (并发: {args.workers})")
    if args.proxy:
        log.info(f"🌐 代理: {args.proxy}")

    stats = {"ok": 0, "fail": 0}

    lock = threading.Lock()
    t0 = time.time()

    if args.workers <= 1:
        # 串行模式
        for i, acc in enumerate(batch, 1):
            _do_one(acc, i, total, args.proxy, stats, lock)
    else:
        # 并行模式
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {}
            for i, acc in enumerate(batch, 1):
                # 同一波次内错开启动
                wave_pos = (i - 1) % args.workers
                delay = wave_pos * random.uniform(1.0, 2.5) if wave_pos > 0 else 0
                fut = pool.submit(_do_one, acc, i, total, args.proxy, stats, lock, delay)
                futs[fut] = acc.email

            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    log.error(f"线程异常 [{futs[fut]}]: {e}")

    elapsed = time.time() - t0
    log.info(f"\n{'='*55}")
    log.info(f"  注册完成")
    log.info(f"{'='*55}")
    log.info(f"  ✅ 成功: {stats['ok']}")
    log.info(f"  ❌ 失败: {stats['fail']}")
    log.info(f"  ⏱️ 耗时: {elapsed:.1f}s ({elapsed/60:.1f}m)")
    log.info(f"  📁 结果: {RESULTS_DIR}")


if __name__ == "__main__":
    main()
