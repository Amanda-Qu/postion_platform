"""Advertised hard-constraint evidence, separate from technical suitability."""
import re
from .parsing import salary_group


def classify_constraints(job, profile):
    city = str(job.get('city') or '').strip()
    target = str(profile.get('preferred_city') or '').strip()
    normalized = city.casefold()
    aliases = [target.casefold()] if target else []
    if target in ('深圳', '深圳市', 'Shenzhen', 'shenzhen'):
        aliases = ['深圳', 'shenzhen']
    matches = bool(aliases and any(x in normalized for x in aliases))
    unknown = not city or normalized in ('未知','待核实','待确认','unknown','n/a') or bool(re.search(r'远程|remote|待定|待确认|不限', normalized))
    location = 'preferred' if matches else 'unknown' if unknown or not target else 'other_city'
    salary = salary_group(job.get('salary_raw', ''), profile.get('salary_target', 40000))
    if location == 'other_city':
        code, label = 'other_city', '其他城市 · 单独考虑'
    elif location == 'unknown':
        code, label = 'unknown', '地点待核实'
    elif salary['group'] == '明确符合':
        code, label = 'meets', '地点及固定月薪明确符合'
    elif salary['group'] == '可能符合':
        code, label = 'possible', '地点符合 · 薪资区间可能符合'
    elif salary['group'] == '低于目标':
        code, label = 'below', '固定月薪低于目标'
    else:
        code, label = 'unknown', '地点符合 · 固定月薪待核实'
    return {'code':code, 'label':label, 'location':location, 'salary':salary,
            'preferred_city':target, 'salary_target':profile.get('salary_target',40000),
            'evidence':{'city':city or '未知','salary_raw':job.get('salary_raw') or '未知'},
            'notes':'仅判断岗位披露的地点与薪资，非技术匹配或录用承诺；薪资未知仍保留调查。'}


def evidence_rank(job):
    """Coverage ranks evidence availability, not unknown skills as failures."""
    coverage=(job.get('analysis') or {}).get('evidence_coverage') or {}
    total=coverage.get('total',0)
    confirmed=coverage.get('confirmed',0)
    return (confirmed/total if total else 0, confirmed)
