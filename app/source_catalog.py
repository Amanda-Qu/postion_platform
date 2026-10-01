"""Curated entrances, not open-web discovery or claims of available matching jobs."""
from copy import deepcopy

CATALOG_VERSION = '2026-10-01'


def company_catalog():
    rows = [
        ('tokenfab', 'TokenFab', 'https://www.tokenfab.cn/join.html', 'tokenfab'),
        ('extremevision', '极视角 ExtremeVision', 'https://www.extremevision.com.cn/join-us/', 'extremevision'),
        ('tencent', '腾讯 Tencent', 'https://careers.tencent.com/search.html', ''),
        ('pudu', '普渡机器人 Pudu', 'https://www.pudurobotics.com/about/job', ''),
        ('ubtech', '优必选 UBTECH', 'https://ubtrobot.zhiye.com/social', ''),
        ('modelbest', '面壁智能 ModelBest', 'https://modelbest.jobs.feishu.cn', ''),
        ('minieye', 'MINIEYE', 'https://www.minieye.cc/career/positions', ''),
        ('bytedance', '字节跳动 ByteDance', 'https://jobs.bytedance.com/experienced', ''),
        ('kuaishou', '快手 Kuaishou', 'https://zhaopin.kuaishou.cn/recruit/e', ''),
        ('weride', '文远知行 WeRide', 'https://zh.weride.ai/zh/careers', ''),
        ('zhipu', '智谱 Zhipu', 'https://www.zhipuai.cn/zh/joinus', ''),
    ]
    return [dict(id=key, name=name+' · 公司招聘官网', company=name,
                 kind=adapter or 'career_portal', url=url, enabled=False,
                 status='待验证' if adapter else '人工查看',
                 ingestion_status='adapter_ready' if adapter else 'portal_pending',
                 message=('已实现专用静态页面解析；启用后仍需验证网络、robots 和页面结构，不保证存在符合约束的职位。'
                          if adapter else '招聘入口已核验，尚无岗位自动读取适配器。请人工查看并导入岗位文字、截图或文件。'),
                 verified_at=CATALOG_VERSION, evidence_url=url,
                 salary_evidence='未核验；不从公司名称或招聘入口推断薪资')
            for key, name, url, adapter in rows]


def merge_catalog(existing, ids):
    """Only append selected missing IDs. Existing user/runtime fields win entirely."""
    catalog = {row['id']: row for row in company_catalog()}
    if len(ids) != len(set(ids)) or any(key not in catalog for key in ids):
        raise ValueError('请选择目录中不重复的来源 id')
    result = deepcopy(existing)
    present = {row['id'] for row in result}
    added = [key for key in ids if key not in present]
    if len(result) + len(added) > 30:
        raise ValueError('来源最多 30 个；请减少本次新增项，已有来源不会被覆盖')
    result.extend(deepcopy(catalog[key]) for key in added)
    return result, added
