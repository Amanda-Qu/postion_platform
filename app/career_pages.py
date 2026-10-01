"""Narrow, read-only adapters for two verified static company career pages.

These pages contain multiple jobs at one real URL and publish no per-job date
or validity guarantee. ``source_identity`` is an internal stable posting key,
not a fabricated job URL; callers must preserve it when ingesting provenance.
All network access goes through sources.safe_fetch with robots checks enabled.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from . import sources


TOKENFAB_URL = "https://www.tokenfab.cn/join.html"
EXTREMEVISION_URL = "https://www.extremevision.com.cn/join-us/"
_URLS = {"tokenfab": TOKENFAB_URL, "extremevision": EXTREMEVISION_URL}
_COMPANIES = {"tokenfab": "TokenFab", "extremevision": "极视角"}
_RESPONSIBILITY_LABELS = {"岗位职责", "工作职责", "职位职责", "工作内容", "岗位描述", "职位描述", "职责"}
_REQUIREMENT_LABELS = {"任职要求", "任职资格", "岗位要求", "职位要求", "技能要求", "基本要求", "优先条件", "加分项"}
_STOP_LABELS = {"薪资", "薪资待遇", "薪酬福利", "福利待遇", "工作地点", "投递方式", "联系方式"}


def _validate_url(url: str, kind: str) -> None:
    """Keep this adapter scoped to its known page, including after redirects."""
    if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\x00-\x20\\]", url):
        raise sources.SourceError("招聘来源链接无效或过长")
    try:
        parsed, expected = urlsplit(url), urlsplit(_URLS[kind])
        host = (parsed.hostname or "").lower()
        allowed_hosts = {expected.hostname, expected.hostname.removeprefix("www.")}
        allowed = (parsed.scheme == "https" and parsed.port in (None, 443)
                   and host in allowed_hosts and not parsed.username and not parsed.password
                   and parsed.path.rstrip("/") == expected.path.rstrip("/")
                   and not parsed.query and not parsed.fragment)
    except (ValueError, TypeError, KeyError):
        allowed = False
    if not allowed:
        raise sources.SourceError("此专用招聘来源只支持已核验的公司招聘页；请检查来源链接或重定向")


def _blocks(element):
    """Preserve each list item/paragraph without duplicating inline descendants."""
    for child in element.children:
        if not getattr(child, "name", None):
            if str(child).strip():
                yield str(child).strip()
        elif child.name in ("script", "style", "svg"):
            continue
        elif child.name in ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            value = child.get_text(" ", strip=True)
            if value:
                yield value
        else:
            yield from _blocks(child)


def _sections(detail) -> dict[str, str]:
    buckets = {"responsibilities": [], "requirements": []}
    counts = {key: 0 for key in buckets}
    current = None
    for line in _blocks(detail):
        pieces = re.split(r"[:：]", line, maxsplit=1)
        label = pieces[0].strip()
        if label in _RESPONSIBILITY_LABELS:
            current = "responsibilities"
        elif label in _REQUIREMENT_LABELS:
            current = "requirements"
        elif label in _STOP_LABELS:
            current = None
            continue
        elif current:
            counts[current] += 1
        if current:
            buckets[current].append(line)
            if label in _RESPONSIBILITY_LABELS | _REQUIREMENT_LABELS and len(pieces) == 2 and pieces[1].strip():
                counts[current] += 1
    if not all(counts.values()):
        raise sources.SourceError("招聘页岗位详情结构变化或内容不完整；无法识别职责和要求，岗位有效状态仍未知")
    qualification_lines = buckets["requirements"]
    # Keep complete original sentences, including preferred/alternative degrees.
    degree = re.compile(r"博士|硕士|本科|大专|学历不限|学士|\b(?:bachelor|master|ph\.?d\.?|degree)\b", re.I)
    years = re.compile(r"\d+(?:\.\d+)?\s*(?:[–—\-至]\s*\d+)?\s*年[^。；\n]{0,40}(?:经验|工作)|工作\s*\d+\s*年|经验不限", re.I)
    return {**{key: "\n".join(value) for key, value in buckets.items()},
            "education": "\n".join(line for line in qualification_lines if degree.search(line)),
            "experience": "\n".join(line for line in qualification_lines if years.search(line))}


def _identity(kind: str, title: str, location: str) -> str:
    normalized = [re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()
                  for value in (title, location)]
    return kind + ":" + hashlib.sha256("\x1f".join(normalized).encode("utf-8")).hexdigest()


def _job(kind: str, page, title: str, city: str, detail, raw: str, source_name: str) -> dict:
    title = title.strip()
    if not title or len(title) > 300:
        raise sources.SourceError("招聘页岗位标题缺失或结构变化，已停止读取")
    result = sources._base_job(company=_COMPANIES[kind], title=title, city=city,
                               **_sections(detail), salary_raw=sources._salary_excerpt(raw),
                               source_url=page.url, source_name=source_name, raw_text=raw)
    result["source_identity"] = _identity(kind, title, city)
    return result


def _finish(jobs: list[dict]) -> list[dict]:
    if not jobs:
        raise sources.SourceError("页面可访问，但未识别到完整岗位；可能为空、结构变化或要求登录，不能据此判断岗位关闭")
    seen = {}
    for job in jobs:
        key = job["source_identity"]
        if key in seen and seen[key] != job:
            raise sources.SourceError("招聘页出现同名同地点但内容不同的岗位，无法安全区分；请手动核实")
        seen[key] = job
    return list(seen.values())


def parse_tokenfab(page: sources.FetchResult, source_name: str = "TokenFab · 官网招聘") -> list[dict]:
    _validate_url(page.url, "tokenfab")
    soup = BeautifulSoup(page.text, "html.parser")
    cards = soup.select(".job-card")
    if len(cards) != len(soup.select(".job-title")) or len(cards) != len(soup.select(".job-detail")):
        raise sources.SourceError("TokenFab 招聘页卡片结构变化：岗位卡片与详情数量不一致，已停止读取")
    jobs = []
    for card in cards:
        titles, details = card.select(".job-title"), card.select(".job-detail")
        if len(titles) != 1 or len(details) != 1:
            raise sources.SourceError("TokenFab 招聘页卡片结构变化：标题或详情缺失，已停止读取")
        title = titles[0]
        # Observed header shape: title wrapper + location wrapper (one span).
        # Never use mailto subjects (they list 深圳 even for multi-city roles),
        # the company address, or a page-wide location as a substitute.
        location_spans = title.parent.parent.select("span")
        city = location_spans[0].get_text(" ", strip=True) if len(location_spans) == 1 else ""
        jobs.append(_job("tokenfab", page, title.get_text(" ", strip=True), city,
                         details[0], card.get_text("\n", strip=True), source_name))
    return _finish(jobs)


def parse_extremevision(page: sources.FetchResult, source_name: str = "极视角 · 官网招聘") -> list[dict]:
    _validate_url(page.url, "extremevision")
    soup = BeautifulSoup(page.text, "html.parser")
    containers = soup.select(".recruitment .list > ul")
    if len(containers) != 1:
        raise sources.SourceError("极视角招聘页岗位列表结构变化或缺失，已停止读取")
    jobs = []
    for card in containers[0].find_all("li", recursive=False):
        titles = card.select(".li-title .name")
        if len(titles) != 1:
            raise sources.SourceError("极视角招聘页岗位标题结构变化或缺失，已停止读取")
        title = titles[0].get_text(" ", strip=True)
        details = card.select(".text")
        # The observed site contains five empty placeholders, not vacancies.
        if title == "更多岗位即将开放" and (not details or not any(d.get_text(strip=True) for d in details)):
            continue
        if len(details) != 1:
            raise sources.SourceError("极视角招聘页岗位详情结构变化或缺失，已停止读取")
        metadata = card.select_one("p.des")
        description = metadata.get_text(" ", strip=True) if metadata else ""
        # The published metadata is city｜department｜hiring channel.
        parts = re.split(r"[｜|]", description)
        city = parts[0].strip() if len(parts) == 3 else ""
        jobs.append(_job("extremevision", page, title, city, details[0],
                         card.get_text("\n", strip=True), source_name))
    return _finish(jobs)


def fetch_career_page(config: dict) -> list[dict]:
    """Fetch only the configured official page, using the shared safety boundary."""
    kind = config.get("kind")
    if kind not in _URLS:
        raise sources.SourceError("未知的专用公司招聘来源")
    url = config.get("url") or _URLS[kind]
    _validate_url(url, kind)
    page = sources.safe_fetch(url, max_bytes=5 * 1024 * 1024)
    parser = parse_tokenfab if kind == "tokenfab" else parse_extremevision
    return parser(page, config.get("name") or f"{_COMPANIES[kind]} · 官网招聘")
