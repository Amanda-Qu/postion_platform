"""Opt-in, bounded company discovery. Search hits are leads, never live jobs.

Only a closed vocabulary of generic skills/cities leaves the server. Search
snippets are untrusted evidence, not company ownership verification.
"""
import hashlib
import http.client
import json
import os
import re
import time
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl, unquote

from .sources import SourceError, RESTRICTED_DOMAINS, _validated_target, _PinnedHTTPSConnection
from .store import now

PROVIDER_URL = 'https://api.search.brave.com/res/v1/web/search'
# Generic query vocabulary, NOT a company catalog. Expand with reviewed public terms.
SKILLS = ('Python', 'C++', 'Java', 'JavaScript', 'TypeScript', 'Rust', 'Golang',
          'PyTorch', 'TensorFlow', 'CUDA', 'ROS', 'SLAM', 'Linux', 'Docker',
          'Kubernetes', 'SQL', 'LLM', 'RAG', 'NLP', '计算机视觉', '机器学习',
          '深度学习', '机器人', '自动驾驶', '嵌入式', '强化学习')
CITIES = ('深圳', '北京', '上海', '杭州', '广州', '苏州', '南京', '成都',
          '武汉', '西安', '合肥', '重庆', '香港', '远程')
DIRECTIONS = ('VLM', '数据闭环', '微调', '多模态', '工业视觉', '具身智能', '算法研发', '训练与部署')
MAX_CANDIDATES = 10


def preview(profile):
    names = ' '.join(s.get('name', '') for s in profile.get('skills', [])
                     if s.get('confirmed') and s.get('level') == 'done')
    skills = [s for s in SKILLS if re.search(r'(?<![A-Za-z0-9_])' + re.escape(s) + r'(?![A-Za-z0-9_])', names, re.I)
              or (not s.isascii() and s in names)][:4]
    cities = [c for c in CITIES if c in profile.get('preferred_city', '')][:2]
    direction_text = ' '.join(profile.get('directions', []))
    directions = [d for d in DIRECTIONS if d.casefold() in direction_text.casefold()][:3]
    keywords = (directions + skills)[:5]
    queries = [f'{city} {" ".join(keywords)} 公司 招聘 官网 careers' for city in cities] if keywords else []
    signature = hashlib.sha256(json.dumps(queries, ensure_ascii=False).encode()).hexdigest()
    configured = bool(os.getenv('BRAVE_SEARCH_API_KEY', '').strip())
    return {'provider':'Brave Search', 'configured':configured, 'skills':skills,
            'cities':cities, 'directions':directions, 'queries':queries, 'query_token':signature,
            'max_requests':len(queries), 'max_candidates':MAX_CANDIDATES,
            'setup': '可选：若已有 Brave Search API 账户，可在服务端 .env 设置 BRAVE_SEARCH_API_KEY 后重启；无需为使用其他功能开通搜索服务。请自行核对服务商套餐和费用，不要在聊天或网页输入密钥。',
            'scope':'只发送下列通用关键词；不发送姓名、联系方式、简历或项目。结果仅为公司入口线索，官网归属、在招岗位和薪资均待核实。每次需手动确认，不参与每日自动更新。'}


def search_brave(query):
    """Fixed official API endpoint, no redirects, capped body, no key in URLs/logs."""
    key = os.getenv('BRAVE_SEARCH_API_KEY', '').strip()
    if not key:
        raise SourceError('搜索服务未配置：请在服务端设置 BRAVE_SEARCH_API_KEY')
    host, addresses = _validated_target(PROVIDER_URL)
    connection = _PinnedHTTPSConnection(host, addresses[0])
    try:
        connection.request('GET', '/res/v1/web/search?' + urlencode({'q':query, 'count':10}),
                           headers={'X-Subscription-Token':key, 'Accept':'application/json', 'Accept-Encoding':'identity'})
        response = connection.getresponse()
        if response.status != 200:
            raise SourceError(f'搜索服务返回 HTTP {response.status}；请检查配置、额度或稍后重试')
        if response.getheader('Content-Encoding', 'identity') not in ('identity', ''):
            raise SourceError('搜索服务返回不支持的压缩响应')
        chunks, size, started = [], 0, time.monotonic()
        while True:
            if time.monotonic() - started > 30:
                raise SourceError('搜索响应超时')
            chunk = response.read1(min(65536, 1024 * 1024 + 1 - size))
            if not chunk: break
            size += len(chunk)
            if size > 1024 * 1024:
                raise SourceError('搜索响应超过大小限制')
            chunks.append(chunk)
        data = b''.join(chunks)
        payload = json.loads(data)
        results = payload.get('web', {}).get('results', [])
        if not isinstance(results, list):
            raise ValueError()
        return results[:10]
    except (OSError, http.client.HTTPException, ValueError, AttributeError) as exc:
        if isinstance(exc, SourceError):
            raise
        raise SourceError('搜索服务连接失败或响应无效；未将任何线索视为已验证') from exc
    finally:
        connection.close()


def candidate_url(value):
    """No fetch here. Actual fetches remain behind sources.safe_fetch SSRF policy."""
    if not isinstance(value, str) or len(value) > 2000 or re.search(r'[\x00-\x20\\]', value):
        return ''
    try:
        p = urlsplit(value)
        host = (p.hostname or '').lower().rstrip('.')
        if p.scheme != 'https' or p.port not in (None,443) or p.username or p.password:
            return ''
        if not re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', host) or host.endswith(('.local','.internal','.localhost','.home','.lan')):
            return ''
        if any(host == d or host.endswith('.'+d) for d in RESTRICTED_DOMAINS):
            return ''
        # Reject credential-bearing links; never redact then represent as evidence.
        # Decode defensively to catch encoded query keys and route parameters.
        decoded = value
        for _ in range(2):
            decoded = unquote(decoded)
        if re.search(r'[\x00-\x20\\]', decoded):
            return ''
        sensitive = r'(?:access[_-]?token|refresh[_-]?token|id[_-]?token|token|api[_-]?key|key|password|passwd|secret|client[_-]?secret|authorization|auth|credential|signature|sig|session(?:id)?|code|x-amz-[a-z-]+|x-goog-[a-z-]+)'
        if re.search(r'(?:[?&#/;])' + sensitive + r'(?:=|/)', decoded, re.I):
            return ''
        # Exact provider URL is the usable link AND provenance, including tenant,
        # postId, and SPA fragments. Normalization belongs only in dedupe_key.
        return value
    except ValueError:
        return ''


def dedupe_key(value):
    """Normalize only known tracking keys; keep all functional query/fragment IDs."""
    url = candidate_url(value)
    if not url: return ''
    p = urlsplit(url)
    query = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('gclid','fbclid','msclkid')]
    return urlunsplit(('https',(p.hostname or '').lower().rstrip('.'),
                      p.path.rstrip('/') or '/',urlencode(sorted(query)),p.fragment))


def discover(plan, existing, search=None):
    search = search or search_brave
    seen = {dedupe_key(s.get('url','')) for s in existing}
    candidates, errors = [], []
    for query in plan['queries'][:2]:
        try:
            rows = search(query)
        except SourceError as exc:
            errors.append(str(exc)); continue
        for row in rows[:10]:
            if not isinstance(row,dict): continue
            url = candidate_url(row.get('url'))
            key = dedupe_key(url)
            if not url or key in seen: continue
            seen.add(key)
            candidates.append({'id':'discovered-'+hashlib.sha256(key.encode()).hexdigest()[:20],
                'dedupe_key':key,
                'url':url, 'title':str(row.get('title',''))[:200],
                'snippet':str(row.get('description',''))[:700], 'provider':'Brave Search',
                'query':query, 'discovered_at':now(), 'verification':'unknown',
                'review_status':'pending', 'evidence_url':url})
            if len(candidates) >= MAX_CANDIDATES: break
        if len(candidates) >= MAX_CANDIDATES: break
    return {'at':now(), 'status':'partial' if errors and candidates else 'failed' if errors else 'complete',
            'queries':plan['queries'], 'candidates':candidates, 'errors':errors}
