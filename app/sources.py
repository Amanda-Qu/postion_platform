"""Public recruitment adapters and conservative, SSRF-resistant HTML retrieval.

Only published, documented feeds are automated. Recruiting platforms without an
approved connector stay available through the user's screenshot/text import.
The caller persists run outcomes; an HTTP failure never marks a job closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import html
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from urllib.parse import quote, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

USER_AGENT = "PersonalCareerDesk/1.0"
MAX_BYTES = 16 * 1024 * 1024
MAX_REDIRECTS = 3
VERIFIED_ON = "2026-09-27"
RESTRICTED_DOMAINS = ("zhipin.com", "liepin.com", "linkedin.com")


class SourceError(ValueError):
    """A fetch/configuration failure suitable for the source status panel."""


@dataclass
class FetchResult:
    url: str
    status_code: int
    headers: dict
    content: bytes

    @property
    def text(self) -> str:
        charset = re.search(r"charset=([\w-]+)", self.headers.get("content-type", ""), re.I)
        encoding = charset.group(1) if charset else "utf-8"
        try:
            return self.content.decode(encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self):
        try:
            return json.loads(self.text)
        except (ValueError, RecursionError) as exc:
            raise SourceError("来源未返回有效 JSON；可能已变更接口或要求登录") from exc


def _validated_target(url: str) -> tuple[str, list[str]]:
    """Reject private/obfuscated addresses, then pin a checked DNS answer.

    All answers must be public: a host resolving to both a public IP and
    127.0.0.1 is rejected, rather than picking whichever answer arrives first.
    The connection below uses the checked IP directly, so rebinding cannot
    make a second DNS resolution redirect the request to the local network.
    """
    if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\x00-\x20\\]", url):
        raise SourceError("链接无效或过长")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        if parsed.scheme.lower() != "https" or parsed.port not in (None, 443):
            raise SourceError("仅支持标准 443 端口的 HTTPS 公开页面")
    except (ValueError, UnicodeError) as exc:
        raise SourceError("链接格式无效") from exc
    if parsed.username is not None or parsed.password is not None or not host:
        raise SourceError("链接不能包含账号密码")
    if "%" in host or ":" in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise SourceError("不接受 IP 字面量或混淆主机地址")
    if "." not in host or not re.search(r"[a-z]", host.rsplit(".", 1)[-1]):
        raise SourceError("不接受 IP 字面量或本地主机地址")
    if host.endswith((".local", ".internal", ".localhost", ".home", ".lan")):
        raise SourceError("禁止访问本地或内部地址")
    if any(host == suffix or host.endswith("." + suffix) for suffix in RESTRICTED_DOMAINS):
        raise SourceError("此平台未接入获授权的自动获取接口；请导入截图、文字或岗位文件")
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceError("域名解析失败；无法据此判断岗位已关闭") from exc
    addresses = list(dict.fromkeys(row[4][0] for row in answers))
    if not addresses:
        raise SourceError("域名没有可用地址")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or getattr(ip, "ipv4_mapped", None) or getattr(ip, "teredo", None) or getattr(ip, "sixtofour", None):
            raise SourceError("禁止访问私网、回环、元数据或特殊网络地址")
    return host, addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str):
        super().__init__(host, timeout=10, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # TLS still verifies the original hostname, never the pinned IP name.
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _request_once(url: str, *, max_bytes: int) -> FetchResult:
    host, addresses = _validated_target(url)
    parsed = urlsplit(url)
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parsed.query:
        path += "?" + quote(parsed.query, safe="%/:?@!$&'()*+,;=-._~")
    last_error = None
    started = time.monotonic()
    for address in addresses[:2]:
        connection = _PinnedHTTPSConnection(host, address)
        try:
            connection.request("GET", path, headers={"Host": host, "User-Agent": USER_AGENT, "Accept": "application/json,text/html,text/plain;q=0.9", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            if headers.get("content-encoding", "identity").lower() not in ("identity", ""):
                raise SourceError("来源忽略了未压缩响应要求，已停止读取以限制解压体积")
            length = headers.get("content-length", "")
            if length.isdigit() and int(length) > max_bytes:
                raise SourceError(f"来源响应超过 {max_bytes // 1024 // 1024} MB 大小限制")
            chunks, total = [], 0
            while True:
                if time.monotonic() - started > 30:
                    raise SourceError("来源响应超过 30 秒读取时限")
                chunk = response.read1(min(64 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise SourceError("来源响应超过大小限制")
                chunks.append(chunk)
            return FetchResult(url, response.status, headers, b"".join(chunks))
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            connection.close()
    raise SourceError("公开来源连接失败或超时；无法据此判断岗位已关闭") from last_error


_robots_cache: dict[str, tuple[float, list[tuple[str, str]], float]] = {}
_last_access: dict[str, float] = {}
_robots_lock = threading.RLock()


def _robots_rules(text: str) -> tuple[list[tuple[str, str]], float]:
    """Read wildcard rules, preferring this crawler's named group if present."""
    groups = []
    agents, rules = [], []
    delay = 1.0
    has_rule = False
    for line in text.splitlines() + ["User-agent: __end__"]:
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if has_rule:
                groups.append((agents, rules, delay))
                agents, rules, delay, has_rule = [], [], 1.0, False
            agents.append(value.lower())
        elif agents and key in ("allow", "disallow", "crawl-delay", "request-rate"):
            has_rule = True
            if key in ("allow", "disallow") and value:
                rules.append((key, value))
            elif key == "crawl-delay":
                try:
                    delay = max(delay, float(value))
                except ValueError:
                    pass
            elif key == "request-rate" and re.fullmatch(r"\d+/\d+", value):
                requests, seconds = map(int, value.split("/"))
                if requests:
                    delay = max(delay, seconds / requests)
    specific = [g for g in groups if any(a != "*" and a in USER_AGENT.lower() for a in g[0])]
    selected = specific or [g for g in groups if "*" in g[0]]
    return [rule for g in selected for rule in g[1]], max([g[2] for g in selected] or [1.0])


def _robots_allows(url: str, rules: list[tuple[str, str]]) -> bool:
    parsed = urlsplit(url)
    target = unquote(parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
    matches = []
    for kind, pattern in rules:
        end = pattern.endswith("$")
        value = pattern[:-1] if end else pattern
        regex = "^" + ".*".join(re.escape(unquote(part)) for part in value.split("*")) + ("$" if end else "")
        if re.search(regex, target):
            matches.append((len(value.replace("*", "")), kind == "allow"))
    return max(matches)[1] if matches else True


def _check_robots(url: str):
    parsed = urlsplit(url)
    origin = f"https://{parsed.netloc}"
    with _robots_lock:
        cached = _robots_cache.get(origin)
        if cached is None or time.monotonic() - cached[0] > 300:
            robots_url = origin + "/robots.txt"
            response = None
            for attempt in range(MAX_REDIRECTS + 1):
                response = _request_once(robots_url, max_bytes=512 * 1024)
                if response.status_code in (301, 302, 303, 307, 308):
                    if attempt == MAX_REDIRECTS:
                        raise SourceError("robots.txt 重定向次数过多，停止获取")
                    robots_url = urljoin(robots_url, response.headers.get("location", ""))
                    continue
                break
            if response.status_code in (404, 410):
                rules, delay = [], 1.0
            elif response.status_code == 200:
                # A login/interstitial HTML response is not an empty robots policy.
                if "<html" in response.text[:1000].lower():
                    raise SourceError("robots.txt 返回了网页或登录页，停止自动读取")
                rules, delay = _robots_rules(response.text)
            else:
                raise SourceError(f"无法核验 robots.txt（HTTP {response.status_code}），停止自动读取")
            cached = (time.monotonic(), rules, delay)
            _robots_cache[origin] = cached
        if not _robots_allows(url, cached[1]):
            raise SourceError("此页面的 robots.txt 不允许自动读取，请手动导入")
        if cached[2] > 30:
            raise SourceError("该网站要求较长抓取间隔；请手动导入或配置专用连接器")
        remaining = cached[2] - (time.monotonic() - _last_access.get(origin, 0))
        if remaining > 0:
            time.sleep(remaining)
        _last_access[origin] = time.monotonic()


def safe_fetch(url: str, *, respect_robots: bool = True, max_bytes: int = MAX_BYTES) -> FetchResult:
    """Fetch HTTPS after checking every redirect; return text/JSON, never execute it.

    Only the two documented API adapters disable HTML robots checks. Application
    routes must not expose that option to an untrusted client.
    """
    for attempt in range(MAX_REDIRECTS + 1):
        _validated_target(url)
        if respect_robots:
            _check_robots(url)
        response = _request_once(url, max_bytes=min(max_bytes, MAX_BYTES))
        if response.status_code in (301, 302, 303, 307, 308):
            if attempt == MAX_REDIRECTS or not response.headers.get("location"):
                raise SourceError("来源重定向过多或缺少目标地址")
            url = urljoin(url, response.headers["location"])
            continue
        if not 200 <= response.status_code < 300:
            raise SourceError(f"最近获取失败：HTTP {response.status_code}；岗位有效状态仍未知")
        return response
    raise SourceError("来源重定向过多")


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(filter(None, (_text(v) for v in value)))
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("description") or value.get("value") or "")
    value = html.unescape(str(value))
    if "<" not in value:
        return value.strip()[:100000]
    return BeautifulSoup(value, "html.parser").get_text("\n", strip=True)[:100000]


def _base_job(**fields) -> dict:
    result = {"company": "未知", "title": "未知", "city": "未知", "salary_raw": "未知", "experience": "未知", "education": "未知", "responsibilities": "未知", "requirements": "未知", "published_at": None, "status_validity": "未知", "source_url": "", "source_name": "", "raw_text": ""}
    for key, value in fields.items():
        if key in result and value not in (None, ""):
            result[key] = value
    # Untrusted structured data may contain javascript:/data: "links". Those
    # remain in raw_text for inspection but must not become clickable links.
    try:
        link = urlsplit(result["source_url"])
        if link.scheme not in ("https", "http") or not link.hostname or link.username or link.password:
            result["source_url"] = ""
    except (ValueError, TypeError):
        result["source_url"] = ""
    return result


def _slug(value, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise SourceError(f"请配置有效的 {field}（仅字母、数字、下划线、连字符）")
    return value


def _salary(value) -> str:
    """Keep currency and interval. Annual compensation is never divided by 12."""
    if not value:
        return "未知"
    if not isinstance(value, dict):
        return _text(value)
    quantity = value.get("value", value)
    if not isinstance(quantity, dict):
        return f"{value.get('currency', '')} {quantity}".strip()
    low, high = quantity.get("minValue", quantity.get("min")), quantity.get("maxValue", quantity.get("max"))
    amount = f"{low if low is not None else '?'}–{high if high is not None else '?'}" if low is not None or high is not None else str(quantity.get("value", "未知"))
    return f"{value.get('currency', '')} {amount} / {quantity.get('unitText', quantity.get('interval', '周期未知'))}".strip()


def _salary_excerpt(content: str) -> str:
    """Preserve an explicit pay sentence; never infer a period/currency.

    Example: 'The US\nbase\nsalary range ... $200,000 - $400,000' keeps
    those lines and nearby context. A missing period stays missing, so the
    later salary classifier must treat this as insufficient information.
    """
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if re.search(r"salary|薪资|月薪|年薪|compensation range", line, re.I) and re.search(r"(?:[$￥¥€£]\s*\d|\d[\d,]*(?:\.\d+)?\s*[kK万]|\d[\d,]{3,})", line):
            return "\n".join(lines[max(0, index - 2): min(len(lines), index + 2)])
    return "未知"


def _greenhouse_sections(markup: str) -> dict[str, str]:
    """Extract labelled sections and verbatim requirement evidence conservatively.

    Headings form a small state machine: Responsibilities -> duties,
    Requirements/Preferred Qualifications -> qualifications, Benefits -> stop.
    For example, 'Master's degree preferred, or equivalent experience' stays a
    complete original sentence; we never turn it into 'Master's required'.
    Inline bold text is joined within its paragraph to preserve that meaning.
    """
    if "<" not in html.unescape(markup):
        blocks = [(line.strip(), False) for line in markup.splitlines() if line.strip()]
    else:
        soup = BeautifulSoup(html.unescape(markup), "html.parser")
        blocks = []

        def collect(node):
            if not getattr(node, "name", None):
                text = str(node).strip()
                if text:
                    blocks.append((text, False))
                return
            if node.name in ("script", "style"):
                return
            if node.name in ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
                text = node.get_text(" ", strip=True)
                strong = node.find(["strong", "b"])
                heading = node.name.startswith("h") or bool(strong and text == strong.get_text(" ", strip=True) and len(text) < 100)
                if text:
                    blocks.append((text, heading))
                return
            for child in node.children:
                collect(child)

        collect(soup)

    responsibility_heading = re.compile(r"(?:key |core |your |primary )?(?:responsibilities|duties)|essential duties(?: and responsibilities)?|what (?:you['’]ll|you will) (?:do|be doing)|the role|about (?:the|this) role|岗位职责|工作职责|职位职责|工作内容|职责", re.I)
    requirement_heading = re.compile(r"(?:(?:minimum|required|basic|preferred|bonus|desired|additional|key|technical) )?(?:qualifications|requirements|skills)|what (?:you['’]ll|you will) bring|what we['’]re looking for|who you are|you (?:may be|might be) a good fit if|nice[- ]to[- ]haves?|bonus points|任职要求|任职资格|岗位要求|职位要求|技能要求|基本要求|优先条件|加分项", re.I)
    stop_heading = re.compile(r"(?:our |your )?(?:benefits|compensation|salary|perks)|compensation and benefits|about (?:us|the company)|why (?:join|work)|how to apply|application process|equal opportunity|公司介绍|关于我们|福利(?:待遇)?|薪资(?:待遇)?|薪酬(?:福利)?|投递方式|工作地点", re.I)
    buckets = {"responsibilities": [], "requirements": []}
    current = None
    for line, html_heading in blocks:
        # Accept a complete heading or an explicit "Heading: inline content".
        clean = re.sub(r"^(?:[一二三四五\d]+[、.．)]\s*)", "", line).strip()
        pieces = re.split(r"[:：]", clean, maxsplit=1)
        label = pieces[0].strip().rstrip(":：")
        if responsibility_heading.fullmatch(label):
            current = "responsibilities"
        elif requirement_heading.fullmatch(label):
            current = "requirements"
        elif stop_heading.fullmatch(label) or html_heading:
            current = None
            continue
        elif re.search(r"^(?:The (?:US )?(?:base )?salary|The pay offered|We are an equal opportunity|Figure is an equal opportunity)", line, re.I):
            current = None
            continue
        if current:
            # Retain original heading labels, especially preferred/bonus clauses.
            buckets[current].append(line)

    requirements = "\n".join(buckets["requirements"])
    candidates = buckets["requirements"] or [line for line, _ in blocks]
    years = re.compile(r"(?:\b\d+(?:\.\d+)?\s*(?:\+|[-–—]\s*\d+)?\s*(?:years?|yrs?)\b.{0,100}\b(?:experience|expertise|background)\b|\bexperience\s*[:：]\s*\d+\+?\s*years?\b|\d+\s*(?:[-–—至]\s*\d+)?\s*年(?:以上)?[^\n。]{0,30}经验|经验不限)", re.I)
    qualitative = re.compile(r"\b(?:extensive|significant|proven|demonstrated)\s+(?:professional\s+|industry\s+)?experience\b", re.I)
    degree = re.compile(r"\b(?:bachelor(?:['’]s|s)?|master(?:['’]s|s)\b|master\s+(?:degree|of)\b|ph\.?d\.?|b\.?s\.?|m\.?s\.?|b\.?sc\.?|m\.?sc\.?)\b|博士|硕士|本科|大专|学历不限", re.I)
    experience = [line for line in candidates if years.search(line) or (requirements and qualitative.search(line))]
    education = [line for line in candidates if degree.search(line)]
    if not requirements:
        # Outside an explicit requirement section, accept degree mentions only
        # with a qualification cue, not company biographies or school clients.
        education = [line for line in education if re.search(r"degree|学历|学位|及以上|要求|优先|需|preferred|required|equivalent", line, re.I)]
    return {"responsibilities": "\n".join(buckets["responsibilities"]), "requirements": requirements,
            "experience": "\n".join(dict.fromkeys(experience)), "education": "\n".join(dict.fromkeys(education))}


def _location(value) -> str:
    if isinstance(value, list):
        return "；".join(dict.fromkeys(filter(None, (_location(item) for item in value))))
    if isinstance(value, dict):
        address = value.get("address", value)
        if isinstance(address, str):
            return address
        return "，".join(dict.fromkeys(filter(None, (_text(address.get(key)) for key in ("addressLocality", "addressRegion", "addressCountry"))))) or _text(value.get("name"))
    return _text(value)


def _jsonld_nodes(value):
    """Handle both standalone JobPosting and @graph/list embedded postings."""
    if isinstance(value, list):
        for child in value:
            yield from _jsonld_nodes(child)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        if types == "JobPosting" or isinstance(types, list) and "JobPosting" in types:
            yield value
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in value:
                yield from _jsonld_nodes(value[key])


def parse_public_page(page: FetchResult, source_name: str = "公司招聘官网") -> list[dict]:
    soup = BeautifulSoup(page.text, "html.parser")
    results, seen = [], set()
    for script in soup.find_all("script", attrs={"type": re.compile(r"^application/ld\+json", re.I)}):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, RecursionError):
            continue
        for node in _jsonld_nodes(data):
            url = urljoin(page.url, node.get("url") or page.url)
            identity = (node.get("title"), url)
            if identity in seen:
                continue
            seen.add(identity)
            validity = "未知"
            expires = node.get("validThrough")
            if expires:
                try:
                    end = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=timezone.utc)
                    if end < datetime.now(timezone.utc):
                        validity = "已关闭"
                except ValueError:
                    pass
            description = _text(node.get("description"))
            responsibilities = _text(node.get("responsibilities"))
            requirements = _text([node.get("qualifications"), node.get("skills")])
            result = _base_job(company=_text(node.get("hiringOrganization")), title=_text(node.get("title")), city=_location(node.get("jobLocation")) or ("远程（范围待确认）" if node.get("jobLocationType") == "TELECOMMUTE" else "未知"), salary_raw=_salary(node.get("baseSalary")), experience=_text(node.get("experienceRequirements")), education=_text(node.get("educationRequirements")), responsibilities=responsibilities or description, requirements=requirements, published_at=node.get("datePosted"), status_validity=validity, source_url=url, source_name=source_name, raw_text=json.dumps(node, ensure_ascii=False))
            results.append(result)
    if not results:
        raise SourceError("页面可访问，但未检测到 JobPosting 结构化数据；请补充文字或截图，不能据此判断岗位关闭")
    return results


def same_source_config(left: dict, right: dict) -> bool:
    """Compare connection inputs, treating absent optional values as empty.

    Model validation adds empty defaults to older source rows. Those defaults
    must not invalidate their connection evidence or an in-flight fetch result.
    Include the legacy slug fallback used by both public API adapters.
    """
    return all((left.get(key) or "") == (right.get(key) or "")
               for key in ("kind", "board", "url", "site", "region", "company", "slug"))


def fetch_source(config: dict) -> list[dict]:
    """Normalize one configured source. Errors propagate for persisted run status."""
    kind = config.get("kind", "")
    if kind in ("boss", "liepin", "linkedin"):
        raise SourceError("该平台暂未接入获授权的职位读取接口；支持手动导入截图、文字和文件")
    if kind == "career_portal":
        raise SourceError("仅核验了公司招聘入口，尚无自动读取适配器；请人工查看并导入岗位")
    if kind in ("tokenfab", "extremevision"):
        from .career_pages import fetch_career_page
        return fetch_career_page(config)
    if kind == "public_page":
        return parse_public_page(safe_fetch(config.get("url", ""), max_bytes=5 * 1024 * 1024), config.get("name") or "公司招聘官网")
    if kind == "greenhouse":
        board = _slug(config.get("board") or config.get("slug"), "Greenhouse board slug")
        root = f"https://boards-api.greenhouse.io/v1/boards/{board}"
        company = config.get("company") or safe_fetch(root, respect_robots=False).json().get("name") or "未知"
        payload = safe_fetch(root + "/jobs?content=true", respect_robots=False).json()
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise SourceError("Greenhouse 返回结构变化：缺少 jobs 列表")
        jobs = []
        for item in payload["jobs"]:
            content = _text(item.get("content"))
            fields = _greenhouse_sections(item.get("content") or "")
            salary = _salary_excerpt(content)
            for metadata in item.get("metadata") or []:
                if re.search(r"salary|薪资", metadata.get("name", ""), re.I) and metadata.get("value"):
                    salary = _text(metadata["value"])
                    break
            jobs.append(_base_job(company=company, title=_text(item.get("title")), city=_text(item.get("location")), salary_raw=salary, **fields, published_at=item.get("first_published"), status_validity="有效", source_url=item.get("absolute_url"), source_name=config.get("name") or "Greenhouse", raw_text=content))
        return jobs
    if kind == "lever":
        site = _slug(config.get("site") or config.get("slug"), "Lever site slug")
        host = "api.eu.lever.co" if config.get("region") == "eu" else "api.lever.co"
        jobs = []
        for page in range(20):
            url = f"https://{host}/v0/postings/{site}?mode=json&skip={page * 100}&limit=100"
            payload = safe_fetch(url, respect_robots=False).json()
            if not isinstance(payload, list):
                raise SourceError("Lever 返回结构变化：预期岗位列表")
            for item in payload:
                content = _text(item.get("descriptionPlain") or item.get("description"))
                sections = [_text(s.get("text")) + "\n" + _text(s.get("content")) for s in item.get("lists", [])]
                # Company is configuration evidence, not guessed from a site slug.
                jobs.append(_base_job(company=config.get("company") or "未知", title=_text(item.get("text")), city=_text(item.get("categories", {}).get("location")), salary_raw=_text(item.get("salaryDescriptionPlain")) or _salary(item.get("salaryRange")), responsibilities=content, requirements="\n".join(sections), status_validity="有效", source_url=item.get("hostedUrl"), source_name=config.get("name") or "Lever", raw_text="\n".join([content, *sections, _text(item.get("additionalPlain"))])))
            if len(payload) < 100:
                return jobs
        raise SourceError("Lever 来源超过单次 2,000 条限制；请缩小来源或增加专用分页配置")
    raise SourceError("未知来源类型，请配置 Greenhouse、Lever 或公开 JobPosting 页面")


def source_catalog() -> list[dict]:
    """Editable defaults: validation proves access, not that jobs fit this user."""
    catalog = [
        {"id": "figureai", "name": "Figure · 具身智能研发（海外）", "kind": "greenhouse", "board": "figureai", "company": "Figure", "enabled": True, "status": "已连接", "reason": "已实测公开 API，含 Perception / Robot Learning 研发职位；主要位于美国，需单列海外机会，不代表符合深圳与薪资约束。", "verified_at": VERIFIED_ON, "evidence_url": "https://boards-api.greenhouse.io/v1/boards/figureai/jobs?content=true"},
        {"id": "boss", "name": "BOSS直聘", "kind": "boss", "enabled": False, "status": "暂不支持", "reason": "尚无获授权的自动职位读取连接器；支持截图、文字、文件导入。已核验 robots 限制搜索查询。", "verified_at": VERIFIED_ON, "evidence_url": "https://www.zhipin.com/robots.txt"},
        {"id": "liepin", "name": "猎聘", "kind": "liepin", "enabled": False, "status": "暂不支持", "reason": "尚无获授权的个人求职搜索接口；企业合作接口不等于个人职位搜索权限。可手动导入。", "verified_at": VERIFIED_ON, "evidence_url": "https://www.liepin.com/robots.txt"},
        {"id": "linkedin", "name": "LinkedIn", "kind": "linkedin", "enabled": False, "status": "暂不支持", "reason": "Talent 接口须申请批准，不是开放职位搜索 API；未申请自动抓取许可。可手动导入。", "verified_at": VERIFIED_ON, "evidence_url": "https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access"},
        {"id": "greenhouse", "name": "公司公开招聘 · Greenhouse", "kind": "greenhouse", "board": "", "company": "", "enabled": False, "status": "需要配置", "reason": "公开 GET API 已实测；填写目标公司 board slug 后启用。已验证示例 anthropic 不代表深圳适配。", "verified_at": VERIFIED_ON, "evidence_url": "https://docs.greenhouse.io/job-board.html", "verified_example": {"board": "anthropic", "company": "Anthropic"}},
        {"id": "lever", "name": "公司公开招聘 · Lever", "kind": "lever", "site": "", "company": "", "enabled": False, "status": "需要配置", "reason": "公开 postings API 已实测；填写目标公司 site slug 和真实公司名后启用。", "verified_at": VERIFIED_ON, "evidence_url": "https://github.com/lever/postings-api", "verified_example": {"site": "palantir", "company": "Palantir Technologies"}},
        {"id": "public_page", "name": "公司官网 · JobPosting", "kind": "public_page", "url": "", "enabled": False, "status": "需要配置", "reason": "支持公开 HTTPS 页面中的 JSON-LD JobPosting，遵守 robots；无结构化数据时提示手动导入。", "verified_at": VERIFIED_ON, "evidence_url": "https://jobs.lever.co/palantir/6ed76ce8-4156-4b60-b120-403538bd66cd"},
    ]
    for source in catalog:
        source["message"] = source["reason"]
        source.setdefault("url", "")
    return catalog
